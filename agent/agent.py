"""
Agent service для MVP.

Граф построен через LangGraph из двух нод:
agent <-> toolnode

agent вызывает LLM, которая выбирает business process и план MCP tools из skills.txt.
toolnode вызывает выбранный MCP tool.
Цикл идёт, пока не выполнены tools из LLM-плана.

ReAct-логика находится в связке agent/toolnode:
- agent получает Thought/Action от LLM;
- toolnode вызывает tool из MCP;
- сам MCP tool проверяет аргументы и риск;
- если нужны данные или подтверждение, tool делает interrupt через state;
- observation результата tool сохраняется в state.

Границы ответственности:
- SecurityClient получает auth context из security service.
- MCPClient выполняет все tools через контрактный слой.
- Агент не проверяет права сам, а использует готовый auth context.
"""

import asyncio
import copy
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import sys
from typing import TypedDict
import uuid
from urllib import error, request

from fastmcp import Client
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph


PROJECT_DIR = Path(__file__).resolve().parents[1]
VLLM_CLIENT_DIR = PROJECT_DIR / "vllm-client"

if str(VLLM_CLIENT_DIR) not in sys.path:
    sys.path.append(str(VLLM_CLIENT_DIR))

from vllm_usage import VLLMClient  # noqa: E402


class AgentState(TypedDict, total=False):
    access_token: str
    thread_id: str
    message: str
    user_arguments: dict
    confirmations: dict
    auth_context: dict
    user_id: str
    intent: str
    project_type: str
    plan: list
    sources: list
    missing_documents: list
    application_structure: list
    planned_tasks: list
    created_tasks: list
    email_draft: dict
    quality: dict
    errors: list
    final_answer: str
    requires_confirmation: bool
    graph_steps: list
    scenario_tools: list
    business_processes: list
    selected_business_process: str
    current_tool_index: int
    next_tool: str
    completed_tools: list
    react_trace: list
    llm_plan: list
    llm_decisions: list
    waiting_for_user: bool
    pending_question: dict
    missing_arguments: list
    pending_confirmation: dict
    stop: bool


class SecurityClient:
    """HTTP-клиент к security service."""

    def __init__(self):
        """Прочитать адрес security service и таймаут из переменных окружения."""

        self.base_url = os.getenv("SECURITY_URL", "http://localhost:8001").rstrip("/")
        self.timeout = float(os.getenv("SECURITY_TIMEOUT_SECONDS", "5"))

    def get_auth_context(self, access_token):
        """Запросить у security service готовый контекст доступа для агента."""

        return self.post_json(
            "/auth/context",
            {"access_token": access_token},
            access_token,
        )

    def post_json(self, path, payload, access_token=None):
        """Отправить POST-запрос в security service и вернуть JSON-ответ."""

        url = self.base_url + path
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}

        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"

        http_request = request.Request(url, data=body, headers=headers, method="POST")
        try:
            with request.urlopen(http_request, timeout=self.timeout) as response:
                raw_data = response.read().decode("utf-8")
                return json.loads(raw_data)
        except (error.HTTPError, error.URLError, TimeoutError, json.JSONDecodeError):
            return {"authenticated": False, "error": "security_unavailable"}


class MCPClient:
    """MCP-клиент для вызова FastMCP tools.

    Если MCP_URL задан, агент вызывает внешний MCP-сервис.
    Если MCP_URL не задан, используется локальный FastMCP app для тестов и dev.
    """

    def __init__(self, transport=None):
        """Выбрать внешний MCP URL или локальный FastMCP app для тестов."""

        self.transport = transport or os.getenv("MCP_URL") or self.load_local_mcp()

    def load_local_mcp(self):
        """Загрузить локальный MCP app, если агент запущен без Docker-сервиса."""

        # Локальный FastMCP app нужен только для unit-тестов и dev-запуска без Docker.
        fake_db_dir = PROJECT_DIR / "fake-db"
        mcp_dir = PROJECT_DIR / "MCP"

        if str(fake_db_dir) not in sys.path:
            sys.path.append(str(fake_db_dir))

        if str(mcp_dir) not in sys.path:
            sys.path.append(str(mcp_dir))

        from mcp_client import mcp  # noqa: WPS433

        return mcp

    def call_tool(self, tool_name, arguments):
        """Синхронно вызвать MCP tool по имени."""

        return asyncio.run(self.call_tool_async(tool_name, arguments))

    async def call_tool_async(self, tool_name, arguments):
        """Асинхронно вызвать MCP tool через FastMCP Client."""

        async with Client(self.transport) as client:
            result = await client.call_tool(tool_name, arguments)
            if result.data is None:
                return {}
            return result.data


class ResearchAgent:
    """Агентный помощник исследователя университета."""

    def __init__(
        self,
        security_client=None,
        mcp_client=None,
        llm_client=None,
        checkpointer=None,
    ):
        """Собрать зависимости агента и построить LangGraph-граф."""

        self.security = security_client or SecurityClient()
        self.mcp = mcp_client or MCPClient()
        self.skills_text = self.load_skills()
        self.business_processes = self.load_business_processes()
        self.scenario_tools = self.default_process_tools()
        self.llm = llm_client or VLLMClient()
        self.system_prompt = self.build_system_prompt()
        self.checkpointer = checkpointer or MemorySaver()
        self.thread_states = {}
        self.graph = self.build_graph()

    def load_skills(self):
        """Прочитать текст сценария из skills.txt."""

        skills_path = Path(__file__).resolve().parent / "skills.txt"
        return skills_path.read_text(encoding="utf-8")

    def load_business_processes(self):
        """Прочитать business processes и tools из skills.txt."""

        processes = []
        process = None
        in_description = False

        def new_process(name):
            process_number = len(processes) + 1
            return {
                "id": f"business_process_{process_number}",
                "name": name,
                "description": "",
                "tools": [],
                "recommended_sequence": [],
            }

        def append_process(current_process):
            if current_process is None:
                return

            if not current_process["recommended_sequence"]:
                current_process["recommended_sequence"] = [
                    tool["name"] for tool in current_process["tools"]
                ]

            processes.append(current_process)

        for line in self.skills_text.splitlines():
            text = line.strip()
            if not text:
                continue

            if text.startswith("Скилл:"):
                append_process(process)
                process = new_process(text.split(":", 1)[1].strip())
                in_description = False
                continue

            if process is None:
                continue

            if text.startswith("process_id:"):
                process["id"] = text.split(":", 1)[1].strip()
                in_description = False
                continue

            if text == "Описание:":
                in_description = True
                continue

            if text.endswith(":"):
                in_description = False
                continue

            if in_description:
                process["description"] = text
                continue

            if text.startswith("- ") and ":" in text:
                tool_name, description = text[2:].split(":", 1)
                process["tools"].append(
                    {
                        "name": tool_name.strip(),
                        "description": description.strip(),
                    }
                )
                continue

            if "->" in text:
                process["recommended_sequence"] = [
                    tool_name.strip() for tool_name in text.split("->")
                ]

        append_process(process)

        return processes

    def default_process_tools(self):
        """Вернуть tools первого business process для обратной совместимости."""

        if not self.business_processes:
            return []

        return self.business_processes[0]["tools"]

    def build_system_prompt(self):
        """Собрать системную инструкцию для LLM-планирования ReAct workflow."""

        processes = json.dumps(self.business_processes, ensure_ascii=False, indent=2)
        return (
            "Ты ReAct-агент университетской AI-платформы.\n"
            "Твоя задача - выбрать business process из skills.txt и сформировать "
            "последовательность MCP tools для выполнения запроса пользователя.\n\n"
            "Доступные business processes и tools:\n"
            f"{processes}\n\n"
            "Полный текст skills.txt:\n"
            f"{self.skills_text}\n\n"
            "Правила:\n"
            "1. Выбери один business_process из списка.\n"
            "2. Верни ordered planned_tools: tools должны быть только из выбранного process.\n"
            "3. auth_context всегда должен быть первым tool плана.\n"
            "4. write_audit должен завершать успешно выполненный business process.\n"
            "5. Не придумывай tools, аргументы, права доступа или источники.\n"
            "6. Используй только данные state, observations и список skills.\n"
            "7. Рискованные действия не подтверждай сам: MCP tool сам остановит "
            "граф и запросит подтверждение пользователя.\n"
            "8. Если пользователь просит часть работы, можно вернуть подмножество tools, "
            "но не пропускай зависимости.\n\n"
            "Формат ответа - только JSON без markdown:\n"
            "{\n"
            '  "thought": "почему выбран этот business process и такой план",\n'
            '  "business_process": "process_id",\n'
            '  "planned_tools": ["ordered", "technical", "tool", "names"]\n'
            "}"
        )

    def build_graph(self):
        """Построить LangGraph из двух нод: agent и toolnode."""

        graph = StateGraph(AgentState)

        graph.add_node("agent", self.agent_node)
        graph.add_node("toolnode", self.toolnode)

        graph.set_entry_point("agent")
        graph.add_conditional_edges(
            "agent",
            self.route_after_agent,
            {
                "toolnode": "toolnode",
                "stop": END,
            },
        )
        graph.add_edge("toolnode", "agent")

        return graph.compile(checkpointer=self.checkpointer)

    def run(
        self,
        access_token,
        message,
        user_arguments=None,
        confirmations=None,
        thread_id=None,
    ):
        """Запустить агентный сценарий для пользовательского запроса."""

        thread_id = thread_id or str(uuid.uuid4())
        auth_context = self.security.get_auth_context(access_token)
        state = self.build_initial_state(
            thread_id,
            access_token,
            message,
            auth_context,
            user_arguments,
            confirmations,
        )

        if thread_id in self.thread_states:
            state = self.build_resume_state(
                self.thread_states[thread_id],
                access_token,
                message,
                auth_context,
                user_arguments,
                confirmations,
            )

        config = {
            "recursion_limit": 50,
            "configurable": {
                "thread_id": thread_id,
            },
        }
        result = self.graph.invoke(state, config)
        self.remember_thread_state(thread_id, result)

        return result

    def build_initial_state(
        self,
        thread_id,
        access_token,
        message,
        auth_context,
        user_arguments=None,
        confirmations=None,
    ):
        """Собрать новый state для первого запуска thread."""

        return {
            "access_token": access_token,
            "thread_id": thread_id,
            "message": message,
            "user_arguments": user_arguments or {},
            "confirmations": confirmations or {},
            "auth_context": auth_context,
            "errors": [],
            "graph_steps": [],
            "react_trace": [],
            "waiting_for_user": False,
            "requires_confirmation": False,
            "planned_tasks": [],
            "created_tasks": [],
            "scenario_tools": self.scenario_tools,
            "business_processes": self.business_processes,
            "selected_business_process": "",
            "current_tool_index": 0,
            "completed_tools": [],
            "llm_plan": [],
            "llm_decisions": [],
        }

    def build_resume_state(
        self,
        saved_state,
        access_token,
        message,
        auth_context,
        user_arguments=None,
        confirmations=None,
    ):
        """Продолжить сохранённый state после tool interrupt без replay шагов."""

        state = copy.deepcopy(saved_state)
        state["access_token"] = access_token
        state["auth_context"] = auth_context

        if message:
            state["message"] = message

        state["user_arguments"] = {
            **state.get("user_arguments", {}),
            **(user_arguments or {}),
        }
        state["confirmations"] = {
            **state.get("confirmations", {}),
            **(confirmations or {}),
        }

        for key in [
            "waiting_for_user",
            "requires_confirmation",
            "stop",
            "pending_question",
            "pending_confirmation",
            "missing_arguments",
            "final_answer",
            "next_tool",
        ]:
            if key in ["waiting_for_user", "requires_confirmation", "stop"]:
                state[key] = False
            else:
                state.pop(key, None)

        state.setdefault("react_trace", []).append(
            {
                "thought": "Возобновление сохранённого thread state после interrupt.",
                "action": "resume",
                "tool_index": state.get("current_tool_index", 0),
            }
        )

        return state

    def remember_thread_state(self, thread_id, state):
        """Сохранить interrupted state или очистить завершённый thread."""

        if state.get("waiting_for_user"):
            self.thread_states[thread_id] = copy.deepcopy(state)
            return

        self.thread_states.pop(thread_id, None)

    def agent_node(self, state):
        """Нода agent через LLM выбирает следующую функцию из skills."""

        state = dict(state)
        state["graph_steps"].append("agent")

        if state.get("stop"):
            state["next_tool"] = "finish"
            return state

        if not state.get("llm_plan"):
            decision = self.plan_business_process(state)
            state["selected_business_process"] = decision["business_process"]
            state["llm_plan"] = decision["planned_tools"]
            state["llm_decisions"].append(decision)
            state["react_trace"].append(
                {
                    "thought": decision["thought"],
                    "business_process": decision["business_process"],
                    "action": "plan_tools",
                    "planned_tools": decision["planned_tools"],
                    "source": decision["source"],
                }
            )

        if state["current_tool_index"] >= len(state.get("llm_plan", [])):
            state["next_tool"] = "finish"
            return state

        tool_name = state["llm_plan"][state["current_tool_index"]]
        state["next_tool"] = tool_name
        state["react_trace"].append(
            {
                "thought": "Выбран следующий tool из LLM-плана.",
                "tool": tool_name,
                "action": "select_tool",
                "source": "llm_plan",
            }
        )

        return state

    def plan_business_process(self, state):
        """Получить LLM-план business process и привести его к валидному виду."""

        raw_response = ""
        try:
            raw_response = self.llm.complete(
                self.system_prompt,
                self.build_llm_user_prompt(state),
            )
            parsed_decision = self.parse_llm_decision(raw_response)
        except Exception as exc:  # noqa: BLE001
            parsed_decision = {
                "validation_error": f"llm_error:{exc.__class__.__name__}",
            }

        return self.validate_llm_decision(state, parsed_decision, raw_response)

    def build_llm_user_prompt(self, state):
        """Собрать компактный state snapshot для выбора следующего tool."""

        snapshot = {
            "user_message": state.get("message", ""),
            "completed_tools": state.get("completed_tools", []),
            "available_business_processes": state.get(
                "business_processes",
                self.business_processes,
            ),
            "current_observations": self.build_observation_snapshot(state),
        }
        return (
            "Выбери business process и сформируй последовательность MCP tools.\n"
            "Ответь строго JSON по системной схеме.\n\n"
            f"State:\n{json.dumps(snapshot, ensure_ascii=False, indent=2)}"
        )

    def build_observation_snapshot(self, state):
        """Оставить для LLM только безопасные и полезные поля state."""

        auth_context = state.get("auth_context", {})
        agent_context = auth_context.get("agent_context", {})
        return {
            "authenticated": auth_context.get("authenticated"),
            "user_id": state.get("user_id"),
            "intent": state.get("intent"),
            "project_type": state.get("project_type"),
            "allowed_actions": agent_context.get("allowed_actions", []),
            "allowed_document_ids": auth_context.get("allowed_document_ids", []),
            "sources": [source.get("id") for source in state.get("sources", [])],
            "missing_documents_count": len(state.get("missing_documents", [])),
            "application_structure_ready": bool(state.get("application_structure")),
            "planned_tasks_count": len(state.get("planned_tasks", [])),
            "created_tasks_count": len(state.get("created_tasks", [])),
            "email_draft_id": state.get("email_draft", {}).get("id"),
            "quality": state.get("quality"),
            "errors": state.get("errors", []),
            "waiting_for_user": state.get("waiting_for_user", False),
            "requires_confirmation": state.get("requires_confirmation", False),
            "stop": state.get("stop", False),
        }

    def parse_llm_decision(self, raw_response):
        """Достать JSON-решение из ответа модели."""

        if not raw_response:
            return {"validation_error": "empty_llm_response"}

        text = raw_response.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:].strip()

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = self.extract_json_object(text)

        if not isinstance(parsed, dict):
            return {"validation_error": "llm_response_is_not_json_object"}

        return parsed

    def extract_json_object(self, text):
        """Найти первый JSON object в ответе, если модель добавила текст вокруг."""

        decoder = json.JSONDecoder()
        for index, character in enumerate(text):
            if character != "{":
                continue

            try:
                parsed, _ = decoder.raw_decode(text[index:])
                return parsed
            except json.JSONDecodeError:
                continue

        return {"validation_error": "json_object_not_found"}

    def validate_llm_decision(self, state, decision, raw_response):
        """Проверить business process и tool plan от LLM."""

        process = self.find_business_process(decision.get("business_process"))
        fallback_used = False
        validation_error = decision.get("validation_error")

        if process is None:
            process = self.default_business_process()
            fallback_used = True
            validation_error = validation_error or "unknown_business_process"

        planned_tools = self.normalize_planned_tools(
            decision.get("planned_tools", []),
            process,
        )
        if not planned_tools:
            planned_tools = list(process.get("recommended_sequence", []))
            fallback_used = True
            validation_error = validation_error or "empty_or_invalid_tool_plan"

        planned_tools = self.apply_plan_safety_rules(planned_tools)

        source = "llm"
        thought = decision.get("thought") or (
            "LLM выбрала business process и сформировала tool plan."
        )
        if fallback_used:
            source = "fallback"
            thought = (
                "LLM-план не прошёл валидацию; выбран fallback plan из skills."
            )

        return {
            "thought": thought,
            "business_process": process["id"],
            "planned_tools": planned_tools,
            "source": source,
            "raw_response": raw_response,
            "validation_error": validation_error,
        }

    def normalize_planned_tools(self, planned_tools, process):
        """Оставить в плане только tools выбранного business process."""

        if not isinstance(planned_tools, list):
            return []

        known_tools = {tool["name"] for tool in process.get("tools", [])}
        normalized_tools = []
        for tool_name in planned_tools:
            if tool_name in known_tools and tool_name not in normalized_tools:
                normalized_tools.append(tool_name)

        return normalized_tools

    def apply_plan_safety_rules(self, planned_tools):
        """Добавить обязательные safety tools без жёсткой бизнес-последовательности."""

        safe_plan = list(planned_tools)

        if "auth_context" in self.allowed_tool_names() and (
            not safe_plan or safe_plan[0] != "auth_context"
        ):
            safe_plan = ["auth_context"] + [
                tool_name for tool_name in safe_plan if tool_name != "auth_context"
            ]

        if "write_audit" in self.allowed_tool_names() and "write_audit" not in safe_plan:
            safe_plan.append("write_audit")

        return safe_plan

    def allowed_tool_names(self):
        """Вернуть множество допустимых имен tools и finish."""

        tool_names = set()
        for process in self.business_processes:
            for tool in process.get("tools", []):
                tool_names.add(tool["name"])
        return tool_names | {"finish"}

    def default_business_process(self):
        """Вернуть первый business process как fallback MVP."""

        if not self.business_processes:
            return {
                "id": "empty_business_process",
                "name": "empty",
                "description": "",
                "tools": [],
                "recommended_sequence": [],
            }

        return self.business_processes[0]

    def find_business_process(self, process_id):
        """Найти business process по id."""

        for process in self.business_processes:
            if process["id"] == process_id:
                return process
        return None

    def next_scenario_tool(self, state):
        """Вернуть следующий tool из LLM-плана или finish."""

        current_index = state.get("current_tool_index", 0)
        tool_plan = state.get("llm_plan", [])
        if current_index >= len(tool_plan):
            return "finish"

        return tool_plan[current_index]

    def remaining_scenario_tools(self, state):
        """Вернуть оставшуюся последовательность tool names."""

        current_index = state.get("current_tool_index", 0)
        tool_plan = state.get("llm_plan", [])
        if tool_plan:
            return tool_plan[current_index:]

        return list(self.default_business_process().get("recommended_sequence", []))

    def route_after_agent(self, state):
        """Решить, идти ли в toolnode или завершать граф."""

        if state.get("next_tool") == "finish":
            return "stop"
        return "toolnode"

    def toolnode(self, state):
        """Нода toolnode вызывает выбранный MCP tool и применяет его результат."""

        state = dict(state)
        state["graph_steps"].append("toolnode")
        tool_name = state["next_tool"]

        result = self.mcp.call_tool(
            tool_name,
            {
                "state": state,
            },
        )
        state = self.apply_mcp_result(state, result)
        state["react_trace"].append(
            {
                "thought": "Observation результата MCP tool сохранён в state.",
                "tool": tool_name,
                "action": "observation",
                "observation_keys": sorted(result.keys()),
            }
        )

        if not state.get("waiting_for_user"):
            state["current_tool_index"] += 1
            state["completed_tools"].append(tool_name)

        return state

    def apply_mcp_result(self, state, result):
        """Слить результат MCP tool в состояние графа."""

        for key, value in result.items():
            if key in ["errors", "react_trace"]:
                state.setdefault(key, [])
                state[key].extend(value)
            else:
                state[key] = value

        return state


class AgentHTTPHandler(BaseHTTPRequestHandler):
    """HTTP-обёртка над агентом."""

    agent = ResearchAgent()

    def do_GET(self):
        """Обработать GET-запросы agent service."""

        if self.path == "/health":
            self.send_json({"status": "ok", "service": "agent"})
            return

        self.send_json({"error": "not_found"}, status=404)

    def do_POST(self):
        """Обработать POST-запрос запуска агента."""

        body = self.read_json()

        if self.path == "/agent/run":
            access_token = self.get_access_token(body)
            result = self.agent.run(
                access_token,
                body.get("message", ""),
                body.get("user_arguments", {}),
                body.get("confirmations", {}),
                body.get("thread_id"),
            )
            self.send_json(result)
            return

        self.send_json({"error": "not_found"}, status=404)

    def get_access_token(self, body):
        """Достать access token из JSON body или Authorization header."""

        if body.get("access_token"):
            return body.get("access_token")

        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            return header.replace("Bearer ", "", 1)

        return None

    def read_json(self):
        """Прочитать JSON body текущего HTTP-запроса."""

        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length)
        if not raw_body:
            return {}
        return json.loads(raw_body.decode("utf-8"))

    def send_json(self, data, status=200):
        """Отправить клиенту JSON-ответ с указанным HTTP-статусом."""

        response = json.dumps(data, ensure_ascii=False).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format, *args):
        """Отключить стандартный access log BaseHTTPRequestHandler."""

        # Отключаем стандартный access log для MVP.
        return


def run_server(host="0.0.0.0", port=8002):
    """Запустить HTTP-сервер agent service."""

    server = HTTPServer((host, port), AgentHTTPHandler)
    print(f"Agent service started on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run_server()
