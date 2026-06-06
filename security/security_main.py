"""
Security service для MVP.

В этом слое уже нет фейкового IAM. Источник пользователя, ролей и групп -
реальный Keycloak. Фейковой остаётся только прикладная БД в fake-db.

Задача слоя:
- принять access token;
- проверить его через Keycloak;
- получить пользователя, роли и группы;
- посчитать доступные действия;
- отфильтровать документы по группам пользователя;
- вернуть agent graph готовый контекст доступа.

Важно: LLM не проверяет права. Она получает только готовый auth context.
"""

from http.server import BaseHTTPRequestHandler, HTTPServer
import base64
import json
import os
from pathlib import Path
import sys
from urllib import error, parse, request


PROJECT_DIR = Path(__file__).resolve().parents[1]
FAKE_DB_DIR = PROJECT_DIR / "fake-db"

if str(FAKE_DB_DIR) not in sys.path:
    sys.path.append(str(FAKE_DB_DIR))

from db_client import DBClient  # noqa: E402


class KeycloakClient:
    """Минимальный HTTP-клиент для Keycloak.

    Для MVP используем стандартную библиотеку Python.
    Если задан KEYCLOAK_CLIENT_SECRET, токен проверяется через introspection.
    Если секрета нет, используем userinfo endpoint.
    """

    def __init__(self):
        """Прочитать настройки подключения к Keycloak из переменных окружения."""

        self.base_url = os.getenv("KEYCLOAK_URL", "http://localhost:8080").rstrip("/")
        self.realm = os.getenv("KEYCLOAK_REALM", "university")
        self.client_id = os.getenv("KEYCLOAK_CLIENT_ID", "university-agent")
        self.client_secret = os.getenv("KEYCLOAK_CLIENT_SECRET", "")
        self.timeout = float(os.getenv("KEYCLOAK_TIMEOUT_SECONDS", "5"))

    def get_user_by_token(self, access_token):
        """Проверить access token и вернуть нормализованного пользователя."""

        if not access_token:
            return None

        if self.client_secret:
            token_data = self.introspect_token(access_token)
        else:
            token_data = self.get_userinfo(access_token)

        if not token_data:
            return None

        return self.normalize_user(token_data)

    def get_userinfo(self, access_token):
        """Получить claims пользователя через Keycloak userinfo endpoint."""

        url = self.openid_url("/userinfo")
        headers = {
            "Authorization": f"Bearer {access_token}",
        }
        return self.get_json(url, headers)

    def introspect_token(self, access_token):
        """Проверить токен через introspection endpoint для confidential client."""

        url = self.openid_url("/token/introspect")
        body = parse.urlencode({"token": access_token}).encode("utf-8")

        basic_auth = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode("utf-8")
        ).decode("utf-8")

        headers = {
            "Authorization": f"Basic {basic_auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        }

        token_data = self.post_json(url, body, headers)
        if not token_data or not token_data.get("active"):
            return None
        return token_data

    def openid_url(self, path):
        """Собрать URL к OpenID Connect endpoint внутри нужного realm."""

        return f"{self.base_url}/realms/{self.realm}/protocol/openid-connect{path}"

    def get_json(self, url, headers):
        """Отправить GET-запрос и вернуть JSON-ответ Keycloak."""

        http_request = request.Request(url, headers=headers, method="GET")
        return self.send_request(http_request)

    def post_json(self, url, body, headers):
        """Отправить POST-запрос и вернуть JSON-ответ Keycloak."""

        http_request = request.Request(url, data=body, headers=headers, method="POST")
        return self.send_request(http_request)

    def send_request(self, http_request):
        """Выполнить HTTP-запрос к Keycloak и вернуть dict или None при ошибке."""

        try:
            with request.urlopen(http_request, timeout=self.timeout) as response:
                raw_data = response.read().decode("utf-8")
                return json.loads(raw_data)
        except (error.HTTPError, error.URLError, TimeoutError, json.JSONDecodeError):
            return None

    def normalize_user(self, token_data):
        """Приводит claims Keycloak к простому формату для нашего сервиса."""

        username = token_data.get("preferred_username") or token_data.get("sub")
        full_name = token_data.get("name") or username

        return {
            "id": username,
            "keycloak_sub": token_data.get("sub"),
            "full_name": full_name,
            "email": token_data.get("email", ""),
            "department": token_data.get("department", ""),
            "roles": self.extract_roles(token_data),
            "groups": self.extract_groups(token_data),
            "permissions": token_data.get("permissions", []),
        }

    def extract_roles(self, token_data):
        """Достать роли из разных claim-структур Keycloak."""

        roles = []

        realm_access = token_data.get("realm_access", {})
        roles.extend(realm_access.get("roles", []))

        resource_access = token_data.get("resource_access", {})
        for client_access in resource_access.values():
            roles.extend(client_access.get("roles", []))

        roles.extend(token_data.get("roles", []))

        return sorted(set(roles))

    def extract_groups(self, token_data):
        """Достать группы пользователя из claims Keycloak."""

        groups = []

        for group in token_data.get("groups", []):
            groups.append(self.normalize_group(group))

        return sorted(set(groups))

    def normalize_group(self, group):
        """Привести имя группы Keycloak к короткому виду без вложенного пути."""

        # Keycloak часто отдаёт группы как /parent/researchers.
        clean_group = group.strip("/")
        if "/" in clean_group:
            return clean_group.split("/")[-1]
        return clean_group


class SecurityService:
    """Простой сервис безопасности для агентного контура."""

    ROLE_PERMISSIONS = {
        "employee": ["documents.read", "email.draft", "audit.write"],
        "researcher": ["documents.read", "email.draft", "audit.write"],
        "grant_applicant": [
            "documents.read",
            "tasks.create",
            "email.draft",
            "audit.write",
        ],
        "grant_officer": [
            "documents.read",
            "tasks.create",
            "email.draft",
            "audit.write",
        ],
    }

    GROUP_PERMISSIONS = {
        "researchers": ["documents.read", "email.draft", "audit.write"],
        "grant_applicants": ["tasks.create"],
        "grant_office": ["documents.read", "tasks.create", "email.draft", "audit.write"],
    }

    def __init__(self, db_client=None, keycloak_client=None):
        """Создать сервис безопасности с fake-db и клиентом Keycloak."""

        self.db = db_client or DBClient()
        self.keycloak = keycloak_client or KeycloakClient()

    def get_auth_context(self, access_token):
        """Возвращает контекст доступа для agent graph."""

        user = self.keycloak.get_user_by_token(access_token)
        if not user:
            return {
                "authenticated": False,
                "error": "invalid_token",
            }

        allowed_actions = self.get_allowed_actions(user)
        documents = self.db.list_documents_for_groups(user["groups"])
        allowed_document_ids = [document["id"] for document in documents]

        context = {
            "authenticated": True,
            "iam": "keycloak",
            "user": {
                "id": user["id"],
                "keycloak_sub": user.get("keycloak_sub"),
                "full_name": user["full_name"],
                "email": user["email"],
                "department": user["department"],
                "roles": user["roles"],
                "groups": user["groups"],
            },
            "permissions": allowed_actions,
            "allowed_document_ids": allowed_document_ids,
            "agent_context": {
                "user_id": user["id"],
                "allowed_actions": allowed_actions,
                "allowed_document_ids": allowed_document_ids,
            },
        }

        self.db.add_audit_log(
            user["id"],
            "security.auth_context_created",
            {
                "iam": "keycloak",
                "document_count": len(allowed_document_ids),
                "action_count": len(allowed_actions),
            },
        )

        return context

    def get_allowed_actions(self, user):
        """Считает действия из ролей/групп Keycloak.

        Если в Keycloak роль уже названа как permission, например tasks.create,
        она тоже попадёт в список действий.
        """

        actions = set(user.get("permissions", []))

        for role in user.get("roles", []):
            if "." in role:
                actions.add(role)
            actions.update(self.ROLE_PERMISSIONS.get(role, []))

        for group in user.get("groups", []):
            actions.update(self.GROUP_PERMISSIONS.get(group, []))

        return sorted(actions)

    def check_permission(self, access_token, permission):
        """Проверяет, можно ли пользователю выполнить действие."""

        context = self.get_auth_context(access_token)
        if not context["authenticated"]:
            return {
                "allowed": False,
                "reason": "invalid_token",
            }

        user_id = context["user"]["id"]
        allowed = permission in context["permissions"]

        self.db.add_audit_log(
            user_id,
            "security.permission_checked",
            {
                "permission": permission,
                "allowed": allowed,
            },
        )

        return {
            "allowed": allowed,
            "permission": permission,
            "user_id": user_id,
        }

    def check_document_access(self, access_token, document_id):
        """Проверяет доступ к одному документу."""

        context = self.get_auth_context(access_token)
        if not context["authenticated"]:
            return {
                "allowed": False,
                "reason": "invalid_token",
            }

        user_id = context["user"]["id"]
        allowed = document_id in context["allowed_document_ids"]

        self.db.add_audit_log(
            user_id,
            "security.document_access_checked",
            {
                "document_id": document_id,
                "allowed": allowed,
            },
        )

        return {
            "allowed": allowed,
            "document_id": document_id,
            "user_id": user_id,
        }

    def filter_documents(self, access_token, document_ids):
        """Оставляет только документы, доступные пользователю."""

        context = self.get_auth_context(access_token)
        if not context["authenticated"]:
            return {
                "authenticated": False,
                "allowed_document_ids": [],
            }

        allowed = []
        for document_id in document_ids:
            if document_id in context["allowed_document_ids"]:
                allowed.append(document_id)

        self.db.add_audit_log(
            context["user"]["id"],
            "security.documents_filtered",
            {
                "requested_count": len(document_ids),
                "allowed_count": len(allowed),
            },
        )

        return {
            "authenticated": True,
            "allowed_document_ids": allowed,
        }


class SecurityHTTPHandler(BaseHTTPRequestHandler):
    """HTTP-обёртка над SecurityService."""

    service = SecurityService()

    def do_GET(self):
        """Обработать GET-запросы security service."""

        if self.path == "/health":
            self.send_json({"status": "ok", "service": "security"})
            return

        self.send_json({"error": "not_found"}, status=404)

    def do_POST(self):
        """Обработать POST endpoints авторизации и проверки доступа."""

        body = self.read_json()
        access_token = self.get_access_token(body)

        if self.path == "/auth/context":
            result = self.service.get_auth_context(access_token)
            self.send_json(result)
            return

        if self.path == "/authz/check":
            result = self.service.check_permission(
                access_token,
                body.get("permission"),
            )
            self.send_json(result)
            return

        if self.path == "/authz/document":
            result = self.service.check_document_access(
                access_token,
                body.get("document_id"),
            )
            self.send_json(result)
            return

        if self.path == "/authz/filter-documents":
            result = self.service.filter_documents(
                access_token,
                body.get("document_ids", []),
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

        # Отключаем стандартный шумный access log для MVP.
        return


def run_server(host="0.0.0.0", port=8001):
    """Запустить HTTP-сервер security service."""

    server = HTTPServer((host, port), SecurityHTTPHandler)
    print(f"Security service started on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run_server()
