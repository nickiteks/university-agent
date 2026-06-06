import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
FAKE_DB_DIR = ROOT_DIR / "fake-db"
AGENT_DIR = ROOT_DIR / "agent"
MCP_DIR = ROOT_DIR / "MCP"

sys.path.append(str(FAKE_DB_DIR))
sys.path.append(str(AGENT_DIR))
sys.path.append(str(MCP_DIR))

import data  # noqa: E402
import mcp_client as mcp_module  # noqa: E402
from agent import MCPClient, ResearchAgent  # noqa: E402


class TestSecurityClient:
    def __init__(self, permissions=None, allowed_document_ids=None):
        self.permissions = permissions or [
            "documents.read",
            "tasks.create",
            "email.draft",
            "audit.write",
        ]
        self.allowed_document_ids = allowed_document_ids or [
            "doc_reg_internal_2026",
            "doc_template_application",
            "doc_instruction_budget",
        ]

    def get_auth_context(self, access_token):
        if access_token == "bad-token":
            return {"authenticated": False, "error": "invalid_token"}

        return {
            "authenticated": True,
            "iam": "keycloak",
            "user": {
                "id": "user_ivanov",
                "full_name": "Иванов Иван Петрович",
                "email": "ivanov@university.local",
                "department": "Кафедра прикладной информатики",
                "roles": ["employee", "researcher", "grant_applicant"],
                "groups": ["researchers", "grant_applicants"],
            },
            "permissions": self.permissions,
            "allowed_document_ids": self.allowed_document_ids,
            "agent_context": {
                "user_id": "user_ivanov",
                "allowed_actions": self.permissions,
                "allowed_document_ids": self.allowed_document_ids,
            },
        }


class TestLLMClient:
    def __init__(self):
        self.calls = []

    def complete(self, system_prompt, user_prompt):
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
            }
        )
        return "Коллеги, прошу согласовать черновик заявки."


class ResearchAgentTest(unittest.TestCase):
    def setUp(self):
        data.TASKS.clear()
        data.EMAIL_DRAFTS.clear()
        data.AUDIT_LOGS.clear()

        self.llm = TestLLMClient()
        mcp_module.llm_client = self.llm
        self.agent = ResearchAgent(
            security_client=TestSecurityClient(),
            mcp_client=MCPClient(),
        )

    def test_agent_prepares_package_without_creating_tasks_before_confirmation(self):
        result = self.agent.run(
            "valid-token",
            "Помоги подготовить пакет для подачи заявки на внутренний научный проект",
        )

        expected_steps = []
        for _ in self.agent.scenario_tools:
            expected_steps.extend(["agent", "toolnode"])
        expected_steps.append("agent")

        expected_tools = [tool["name"] for tool in self.agent.scenario_tools]

        self.assertEqual(expected_steps, result["graph_steps"])
        self.assertEqual(expected_tools, result["completed_tools"])
        self.assertEqual({"agent", "toolnode"}, set(result["graph_steps"]))
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual([], result["created_tasks"])
        self.assertEqual(1, len(result["planned_tasks"]))
        self.assertEqual("email_draft_1", result["email_draft"]["id"])
        self.assertEqual(1, len(self.llm.calls))
        self.assertIn("doc_reg_internal_2026", [source["id"] for source in result["sources"]])

    def test_agent_creates_tasks_after_confirmation(self):
        result = self.agent.run(
            "valid-token",
            "Подготовь заявку и создай задачи соавторам",
            confirmations={"prepare_tasks": True},
        )

        self.assertEqual(1, len(result["created_tasks"]))
        self.assertEqual("task_1", result["created_tasks"][0]["id"])
        self.assertFalse(result["requires_confirmation"])

    def test_tool_interrupts_before_task_creation_without_confirmation(self):
        result = self.agent.run(
            "valid-token",
            "Подготовь заявку и создай задачи соавторам",
        )

        self.assertTrue(result["waiting_for_user"])
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual("confirmation", result["pending_question"]["type"])
        self.assertEqual("prepare_tasks", result["pending_question"]["tool"])
        self.assertEqual([], result["created_tasks"])
        self.assertNotIn("prepare_tasks", result["completed_tools"])

    def test_tool_interrupts_when_required_argument_is_missing(self):
        result = self.agent.run("valid-token", "")

        self.assertTrue(result["waiting_for_user"])
        self.assertFalse(result["requires_confirmation"])
        self.assertEqual("missing_arguments", result["pending_question"]["type"])
        self.assertEqual("search_knowledge", result["pending_question"]["tool"])
        self.assertEqual("query", result["missing_arguments"][0]["name"])
        self.assertNotIn("search_knowledge", result["completed_tools"])

    def test_tool_uses_user_argument_after_interrupt(self):
        result = self.agent.run(
            "valid-token",
            "",
            user_arguments={"query": "регламент внутреннего научного проекта"},
        )

        self.assertFalse(result["waiting_for_user"])
        self.assertIn("search_knowledge", result["completed_tools"])
        self.assertIn("doc_reg_internal_2026", [source["id"] for source in result["sources"]])

    def test_agent_does_not_create_tasks_without_permission(self):
        agent = ResearchAgent(
            security_client=TestSecurityClient(permissions=["documents.read", "email.draft"]),
            mcp_client=MCPClient(),
        )

        result = agent.run(
            "valid-token",
            "Создай задачи соавторам",
            confirmations={"prepare_tasks": True},
        )

        self.assertEqual([], result["created_tasks"])
        self.assertIn("Нет права tasks.create для создания задач.", result["errors"])

    def test_agent_stops_on_invalid_token(self):
        result = self.agent.run("bad-token", "Подготовь заявку")

        self.assertTrue(result["stop"])
        self.assertIn("invalid_token", result["errors"])
        self.assertEqual(["agent", "toolnode", "agent"], result["graph_steps"])
        self.assertEqual(["auth_context"], result["completed_tools"])
        self.assertEqual("Нет доступа: токен не прошёл проверку Keycloak.", result["final_answer"])

    def test_agent_uses_only_allowed_sources(self):
        result = self.agent.run("valid-token", "Найди регламенты и памятки")
        source_ids = [source["id"] for source in result["sources"]]

        self.assertNotIn("doc_private_grant_office", source_ids)


if __name__ == "__main__":
    unittest.main()
