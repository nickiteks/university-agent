"""
Agent service для MVP.

Граф построен через LangGraph из двух нод:
agent <-> toolnode

agent вызывает LLM, которая выбирает следующий MCP tool из skills.txt.
toolnode вызывает выбранный MCP tool.
Цикл идёт, пока не выполнены все функции сценария.

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
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import sys
from typing import TypedDict
from urllib import error, request

from fastmcp import Client
from langgraph.graph import END, StateGraph


PROJECT_DIR = Path(__file__).resolve().parents[1]
VLLM_CLIENT_DIR = PROJECT_DIR / "vllm-client"

if str(VLLM_CLIENT_DIR) not in sys.path:
    sys.path.append(str(VLLM_CLIENT_DIR))

from vllm_usage import VLLMClient  # noqa: E402


class AgentState(TypedDict, total=False):
    access_token: str
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

    def __init__(self, security_client=None, mcp_client=None, llm_client=None):
        """Собрать зависимости агента и построить LangGraph-граф."""

        self.security = security_client or SecurityClient()
        self.mcp = mcp_client or MCPClient()
        self.skills_text = self.load_skills()
        self.scenario_tools = self.load_scenario_tools()
        self.llm = llm_client or VLLMClient()
        self.system_prompt = self.build_system_prompt()
        self.graph = self.build_graph()

    def load_skills(self):
        """Прочитать текст сценария из skills.txt."""

        skills_path = Path(__file__).resolve().parent / "skills.txt"
        return skills_path.read_text(encoding="utf-8")

    def load_scenario_tools(self):
        """Связать шаги skills.txt с техническими именами MCP/tools функций."""

        # Технические имена tool идут в том же порядке, что шаги в skills.txt.
        tool_names = [
            "auth_context",
            "detect_intent",
            "search_knowledge",
            "check_requirements",
            "list_missing_documents",
            "prepare_application_structure",
            "prepare_tasks",
            "prepare_email_draft",
            "quality_check",
            "write_audit",
        ]

        descriptions = []
        for line in self.skills_text.splitlines():
            text = line.strip()
            step_number = len(descriptions) + 1

            if text.startswith(f"{step_number}."):
                description = text.split(".", 1)[1].strip()
                descriptions.append(description)

        scenario_tools = []
        for index, tool_name in enumerate(tool_names):
            scenario_tools.append(
                {
                    "name": tool_name,
                    "description": descriptions[index],
                }
            )

        return scenario_tools

    def build_system_prompt(self):
        """Собрать системную инструкцию для LLM-выбора ReAct action."""

        skills = json.dumps(self.scenario_tools, ensure_ascii=False, indent=2)
        return (
            "Ты ReAct-агент университетской AI-платформы.\n"
            "Твоя задача - выбирать следующий MCP tool из skills и строить "
            "последовательность действий для выполнения запроса пользователя.\n\n"
            "Доступные skills/tools:\n"
            f"{skills}\n\n"
            "Правила:\n"
            "1. Работай в цикле Thought -> Action -> Observation.\n"
            "2. На каждом шаге выбирай ровно один tool из списка skills или finish.\n"
            "3. Сначала всегда выбирай auth_context, чтобы проверить auth context.\n"
            "4. Не придумывай tools, аргументы, права доступа или источники.\n"
            "5. Используй только данные state, observations и список skills.\n"
            "6. Рискованные действия не подтверждай сам: MCP tool сам остановит "
            "граф и запросит подтверждение пользователя.\n"
            "7. finish выбирай только если все нужные skills выполнены или state.stop=true.\n\n"
            "Формат ответа - только JSON без markdown:\n"
            "{\n"
            '  "thought": "почему выбран этот tool",\n'
            '  "tool": "technical_tool_name_or_finish",\n'
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

        return graph.compile()

    def run(
        self,
        access_token,
        message,
        user_arguments=None,
        confirmations=None,
    ):
        """Запустить агентный сценарий для пользовательского запроса."""

        auth_context = self.security.get_auth_context(access_token)
        state = {
            "access_token": access_token,
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
            "current_tool_index": 0,
            "completed_tools": [],
            "llm_plan": [],
            "llm_decisions": [],
        }
        return self.graph.invoke(state, {"recursion_limit": 50})

    def agent_node(self, state):
        """Нода agent через LLM выбирает следующую функцию из skills."""

        state = dict(state)
        state["graph_steps"].append("agent")

        if state.get("stop"):
            state["next_tool"] = "finish"
            return state

        if state["current_tool_index"] >= len(state["scenario_tools"]):
            state["next_tool"] = "finish"
            return state

        decision = self.select_next_tool(state)
        state["next_tool"] = decision["tool"]
        state["llm_plan"] = decision.get("planned_tools", [])
        state["llm_decisions"].append(decision)
        state["react_trace"].append(
            {
                "thought": decision["thought"],
                "tool": decision["tool"],
                "action": "select_tool",
                "source": decision["source"],
            }
        )

        return state

    def select_next_tool(self, state):
        """Получить LLM-решение и привести его к валидному ReAct action."""

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
            "remaining_tools": self.remaining_scenario_tools(state),
            "current_observations": self.build_observation_snapshot(state),
            "existing_llm_plan": state.get("llm_plan", []),
        }
        return (
            "Выбери следующий ReAct Action для текущего состояния.\n"
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
        """Проверить tool от LLM и при необходимости заменить fallback action."""

        fallback_tool = self.next_scenario_tool(state)
        requested_tool = decision.get("tool")
        planned_tools = self.normalize_planned_tools(decision.get("planned_tools", []))
        validation_error = decision.get("validation_error")

        if state.get("stop"):
            requested_tool = "finish"
        elif requested_tool != fallback_tool:
            validation_error = validation_error or (
                f"expected_{fallback_tool}_got_{requested_tool}"
            )
            requested_tool = fallback_tool

        if requested_tool not in self.allowed_tool_names():
            validation_error = validation_error or f"unknown_tool:{requested_tool}"
            requested_tool = fallback_tool

        if not planned_tools:
            planned_tools = self.remaining_scenario_tools(state)

        source = "llm"
        thought = decision.get("thought") or "LLM выбрала следующий tool из skills."
        if validation_error:
            source = "fallback"
            thought = (
                "LLM-решение не прошло валидацию; выбран следующий валидный "
                "tool из skills."
            )

        return {
            "thought": thought,
            "tool": requested_tool,
            "planned_tools": planned_tools,
            "source": source,
            "raw_response": raw_response,
            "validation_error": validation_error,
        }

    def normalize_planned_tools(self, planned_tools):
        """Оставить в плане только известные technical tool names."""

        if not isinstance(planned_tools, list):
            return []

        known_tools = self.allowed_tool_names()
        normalized_tools = []
        for tool_name in planned_tools:
            if tool_name in known_tools and tool_name not in normalized_tools:
                normalized_tools.append(tool_name)

        return normalized_tools

    def allowed_tool_names(self):
        """Вернуть множество допустимых имен tools и finish."""

        return {tool["name"] for tool in self.scenario_tools} | {"finish"}

    def next_scenario_tool(self, state):
        """Вернуть следующий обязательный tool в последовательности skills."""

        current_index = state.get("current_tool_index", 0)
        scenario_tools = state.get("scenario_tools", self.scenario_tools)
        if current_index >= len(scenario_tools):
            return "finish"

        return scenario_tools[current_index]["name"]

    def remaining_scenario_tools(self, state):
        """Вернуть оставшуюся последовательность technical tool names."""

        current_index = state.get("current_tool_index", 0)
        scenario_tools = state.get("scenario_tools", self.scenario_tools)
        return [tool["name"] for tool in scenario_tools[current_index:]]

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
