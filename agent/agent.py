"""
Agent service для MVP.

Граф построен через LangGraph из двух нод:
agent <-> toolnode

agent выбирает следующий MCP tool из skills.txt.
toolnode вызывает выбранный MCP tool.
Цикл идёт, пока не выполнены все функции сценария.

ReAct-логика находится в связке agent/toolnode:
- agent выбирает следующий tool;
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

    def __init__(self, security_client=None, mcp_client=None):
        """Собрать зависимости агента и построить LangGraph-граф."""

        self.security = security_client or SecurityClient()
        self.mcp = mcp_client or MCPClient()
        self.skills_text = self.load_skills()
        self.scenario_tools = self.load_scenario_tools()
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
        }
        return self.graph.invoke(state, {"recursion_limit": 50})

    def agent_node(self, state):
        """Нода agent выбирает следующую функцию из сценария."""

        state = dict(state)
        state["graph_steps"].append("agent")

        if state.get("stop"):
            state["next_tool"] = "finish"
            return state

        if state["current_tool_index"] >= len(state["scenario_tools"]):
            state["next_tool"] = "finish"
            return state

        current_tool = state["scenario_tools"][state["current_tool_index"]]
        state["next_tool"] = current_tool["name"]
        state["react_trace"].append(
            {
                "thought": "Выбран следующий tool из сценария.",
                "tool": current_tool["name"],
                "action": "select_tool",
            }
        )

        return state

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
