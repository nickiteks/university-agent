import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
FAKE_DB_DIR = ROOT_DIR / "fake-db"
sys.path.append(str(FAKE_DB_DIR))

import data  # noqa: E402
from db_client import FakeDBClient  # noqa: E402


class FakeDBClientTest(unittest.TestCase):
    def setUp(self):
        data.TASKS.clear()
        data.EMAIL_DRAFTS.clear()
        data.AUDIT_LOGS.clear()
        self.db = FakeDBClient()

    def test_documents_are_filtered_by_user_groups(self):
        researcher_docs = self.db.list_documents_for_user("user_ivanov")
        guest_docs = self.db.list_documents_for_user("user_guest")

        researcher_doc_ids = [document["id"] for document in researcher_docs]

        self.assertIn("doc_reg_internal_2026", researcher_doc_ids)
        self.assertNotIn("doc_private_grant_office", researcher_doc_ids)
        self.assertEqual([], guest_docs)

    def test_search_documents_returns_relevant_documents(self):
        documents = self.db.search_documents("user_ivanov", "бюджет проекта")
        document_ids = [document["id"] for document in documents]

        self.assertIn("doc_instruction_budget", document_ids)

    def test_missing_documents_are_built_from_project_requirements(self):
        missing = self.db.get_missing_documents("user_ivanov", "internal_research")
        missing_codes = [document["code"] for document in missing]

        self.assertEqual(
            ["project_budget", "calendar_plan", "manager_approval"],
            missing_codes,
        )

    def test_task_and_email_draft_are_audited(self):
        task = self.db.create_task(
            "user_ivanov",
            "user_sidorov",
            "Подготовить календарный план",
            "Нужен календарный план для заявки",
        )
        draft = self.db.create_email_draft(
            "user_ivanov",
            ["petrova@university.local"],
            "Согласование заявки",
            "Коллеги, прошу согласовать черновик заявки.",
        )
        audit_logs = self.db.list_audit_logs("user_ivanov")

        self.assertEqual("task_1", task["id"])
        self.assertEqual("email_draft_1", draft["id"])
        actions = [log["action"] for log in audit_logs]
        self.assertEqual(["task.created", "email.draft_created"], actions)


if __name__ == "__main__":
    unittest.main()
