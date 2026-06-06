import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
FAKE_DB_DIR = ROOT_DIR / "fake-db"
SECURITY_DIR = ROOT_DIR / "security"

sys.path.append(str(FAKE_DB_DIR))
sys.path.append(str(SECURITY_DIR))

import data  # noqa: E402
from security_main import KeycloakClient, SecurityService  # noqa: E402


class TestKeycloakClient:
    """Тестовый клиент вместо настоящего Keycloak.

    Это не production-код. Он нужен только для unit-тестов security слоя.
    """

    USERS = {
        "valid-researcher-token": {
            "id": "user_ivanov",
            "keycloak_sub": "kc-sub-ivanov",
            "full_name": "Иванов Иван Петрович",
            "email": "ivanov@university.local",
            "department": "Кафедра прикладной информатики",
            "roles": ["employee", "researcher", "grant_applicant"],
            "groups": ["researchers", "grant_applicants"],
            "permissions": [],
        },
        "limited-token": {
            "id": "user_sidorov",
            "keycloak_sub": "kc-sub-sidorov",
            "full_name": "Сидоров Олег Викторович",
            "email": "sidorov@university.local",
            "department": "Кафедра математики",
            "roles": ["employee"],
            "groups": ["researchers"],
            "permissions": [],
        },
        "grant-office-token": {
            "id": "user_petrova",
            "keycloak_sub": "kc-sub-petrova",
            "full_name": "Петрова Анна Сергеевна",
            "email": "petrova@university.local",
            "department": "Проектный офис",
            "roles": ["employee", "grant_officer"],
            "groups": ["grant_office"],
            "permissions": [],
        },
    }

    def get_user_by_token(self, access_token):
        return self.USERS.get(access_token)


class SecurityServiceTest(unittest.TestCase):
    def setUp(self):
        data.AUDIT_LOGS.clear()
        self.security = SecurityService(keycloak_client=TestKeycloakClient())

    def test_auth_context_is_built_from_keycloak_user(self):
        context = self.security.get_auth_context("valid-researcher-token")

        self.assertTrue(context["authenticated"])
        self.assertEqual("keycloak", context["iam"])
        self.assertEqual("user_ivanov", context["user"]["id"])
        self.assertEqual("kc-sub-ivanov", context["user"]["keycloak_sub"])
        self.assertIn("researchers", context["user"]["groups"])
        self.assertIn("doc_reg_internal_2026", context["allowed_document_ids"])

    def test_invalid_token_is_not_authenticated(self):
        context = self.security.get_auth_context("bad-token")

        self.assertFalse(context["authenticated"])
        self.assertEqual("invalid_token", context["error"])

    def test_permission_is_checked_from_keycloak_roles_and_groups(self):
        allowed = self.security.check_permission(
            "valid-researcher-token",
            "tasks.create",
        )
        denied = self.security.check_permission("limited-token", "tasks.create")

        self.assertTrue(allowed["allowed"])
        self.assertFalse(denied["allowed"])

    def test_private_document_is_not_allowed_for_researcher(self):
        result = self.security.check_document_access(
            "valid-researcher-token",
            "doc_private_grant_office",
        )

        self.assertFalse(result["allowed"])

    def test_grant_office_group_can_read_private_document(self):
        result = self.security.check_document_access(
            "grant-office-token",
            "doc_private_grant_office",
        )

        self.assertTrue(result["allowed"])

    def test_document_filter_returns_only_allowed_documents(self):
        result = self.security.filter_documents(
            "valid-researcher-token",
            [
                "doc_reg_internal_2026",
                "doc_private_grant_office",
            ],
        )

        self.assertEqual(["doc_reg_internal_2026"], result["allowed_document_ids"])


class KeycloakClientNormalizeTest(unittest.TestCase):
    def test_keycloak_claims_are_normalized(self):
        client = KeycloakClient()
        user = client.normalize_user(
            {
                "sub": "keycloak-sub",
                "preferred_username": "user_ivanov",
                "name": "Иванов Иван Петрович",
                "email": "ivanov@university.local",
                "realm_access": {"roles": ["employee", "researcher"]},
                "resource_access": {
                    "university-agent": {"roles": ["grant_applicant"]}
                },
                "groups": ["/university/researchers", "/grant_applicants"],
            }
        )

        self.assertEqual("user_ivanov", user["id"])
        self.assertEqual("keycloak-sub", user["keycloak_sub"])
        self.assertEqual(["employee", "grant_applicant", "researcher"], user["roles"])
        self.assertEqual(["grant_applicants", "researchers"], user["groups"])


if __name__ == "__main__":
    unittest.main()
