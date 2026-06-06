import asyncio
import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
FAKE_DB_DIR = ROOT_DIR / "fake-db"
MCP_DIR = ROOT_DIR / "MCP"

sys.path.append(str(FAKE_DB_DIR))
sys.path.append(str(MCP_DIR))

from fastmcp import Client  # noqa: E402
import data  # noqa: E402
import mcp_client as mcp_module  # noqa: E402
from mcp_client import mcp  # noqa: E402


def call_tool(tool_name, arguments):
    async def run():
        async with Client(mcp) as client:
            result = await client.call_tool(tool_name, arguments)
            return result.data

    return asyncio.run(run())


class TestLLMClient:
    def complete(self, system_prompt, user_prompt):
        return "Черновик письма"


class MCPToolsTest(unittest.TestCase):
    def setUp(self):
        data.TASKS.clear()
        data.EMAIL_DRAFTS.clear()
        data.AUDIT_LOGS.clear()
        mcp_module.llm_client = TestLLMClient()

    def test_search_knowledge_uses_allowed_document_ids(self):
        result = call_tool(
            "search_knowledge",
            {
                "state": {
                    "auth_context": {
                        "allowed_document_ids": ["doc_reg_internal_2026"],
                    },
                    "user_arguments": {
                        "query": "регламент проект",
                    },
                    "message": "",
                }
            },
        )

        self.assertEqual(["doc_reg_internal_2026"], [source["id"] for source in result["sources"]])

    def test_prepare_tasks_waits_for_confirmation(self):
        result = call_tool(
            "prepare_tasks",
            {
                "state": {
                    "user_id": "user_ivanov",
                    "message": "Подготовь задачи соавторам",
                    "missing_documents": [{"code": "project_budget", "title": "Бюджет проекта"}],
                    "confirmations": {},
                    "auth_context": {
                        "agent_context": {
                            "allowed_actions": ["tasks.create"],
                        }
                    },
                }
            },
        )

        self.assertEqual(1, len(result["planned_tasks"]))
        self.assertEqual([], result["created_tasks"])
        self.assertTrue(result["requires_confirmation"])

    def test_prepare_tasks_creates_task_after_confirmation(self):
        result = call_tool(
            "prepare_tasks",
            {
                "state": {
                    "user_id": "user_ivanov",
                    "message": "Создай задачи соавторам",
                    "missing_documents": [{"code": "project_budget", "title": "Бюджет проекта"}],
                    "confirmations": {
                        "prepare_tasks": True,
                    },
                    "auth_context": {
                        "agent_context": {
                            "allowed_actions": ["tasks.create"],
                        }
                    },
                }
            },
        )

        self.assertEqual([], result["planned_tasks"])
        self.assertEqual("task_1", result["created_tasks"][0]["id"])
        self.assertFalse(result["requires_confirmation"])

    def test_prepare_email_draft_creates_only_draft(self):
        result = call_tool(
            "prepare_email_draft",
            {
                "state": {
                    "user_id": "user_ivanov",
                    "missing_documents": [{"code": "project_budget", "title": "Бюджет проекта"}],
                    "sources": [],
                    "auth_context": {
                        "agent_context": {
                            "allowed_actions": ["email.draft"],
                        }
                    },
                }
            },
        )

        self.assertEqual("email_draft_1", result["email_draft"]["id"])
        self.assertEqual("draft", result["email_draft"]["status"])


if __name__ == "__main__":
    unittest.main()
