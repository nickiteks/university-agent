"""
Простой клиент для fake-db.

Клиент специально написан без сложных абстракций: методы читают и меняют
словари из data.py. Позже этот слой можно заменить на PostgreSQL/MinIO/Qdrant.
"""

from pathlib import Path
import sys


# Папка называется fake-db, поэтому добавляем её в путь импорта.
CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.append(str(CURRENT_DIR))

from data import (  # noqa: E402
    AUDIT_LOGS,
    DOCUMENT_REQUIREMENTS,
    DOCUMENTS,
    EMAIL_DRAFTS,
    PROJECT_TYPES,
    TASKS,
    USER_STATES,
    USERS,
)


class FakeDBClient:
    """Минимальный клиент для работы с фейковыми данными."""

    def get_user(self, user_id):
        """Вернуть пользователя по id или None, если такого пользователя нет."""

        return USERS.get(user_id)

    def list_users(self):
        """Вернуть список всех пользователей из fake-db."""

        return list(USERS.values())

    def get_user_state(self, user_id):
        """Вернуть сохранённое состояние пользователя для агентного сценария."""

        return USER_STATES.get(user_id, {})

    def update_user_state(self, user_id, new_data):
        """Обновить состояние пользователя и вернуть новое состояние."""

        # Если состояния ещё нет, создаём пустое.
        state = USER_STATES.setdefault(user_id, {})
        state.update(new_data)
        return state

    def get_user_groups(self, user_id):
        """Вернуть группы пользователя, которые используются для доступа к документам."""

        user = self.get_user(user_id)
        if not user:
            return []
        return user["groups"]

    def list_documents_for_user(self, user_id):
        """Вернуть активные документы, доступные конкретному пользователю."""

        user_groups = self.get_user_groups(user_id)
        return self.list_documents_for_groups(user_groups)

    def list_documents_for_groups(self, groups):
        """Вернуть активные документы, доступные хотя бы одной из групп."""

        # Группы приходят из IAM/Keycloak, а ACL документов лежит в fake-db.
        result = []

        for document in DOCUMENTS.values():
            has_access = bool(set(groups) & set(document["access_groups"]))
            if document["status"] == "active" and has_access:
                result.append(document)

        return result

    def get_document(self, user_id, document_id):
        """Вернуть документ, если пользователь имеет к нему доступ."""

        for document in self.list_documents_for_user(user_id):
            if document["id"] == document_id:
                return document
        return None

    def search_documents(self, user_id, query):
        """Найти документы пользователя по простому поиску в тексте."""

        # Простейший поиск по словам. Для MVP этого достаточно.
        words = [word.lower().strip(".,!?") for word in query.split()]
        documents = self.list_documents_for_user(user_id)
        return self.search_in_documents(documents, words)

    def search_documents_by_ids(self, document_ids, query):
        """Найти документы только внутри заранее разрешённого списка ids."""

        # Агент передаёт сюда только ids, разрешённые security слоем.
        words = [word.lower().strip(".,!?") for word in query.split()]
        documents = []
        for document_id in document_ids:
            if document_id in DOCUMENTS:
                documents.append(DOCUMENTS[document_id])

        return self.search_in_documents(documents, words)

    def search_in_documents(self, documents, words):
        """Выполнить простой поиск слов по заголовку, содержимому и ключевым словам."""

        result = []

        for document in documents:
            text = " ".join(
                [
                    document["title"],
                    document["content"],
                    " ".join(document["keywords"]),
                ]
            ).lower()

            if any(word in text for word in words):
                result.append(document)

        return result

    def get_project_type(self, project_type_id):
        """Вернуть описание типа проекта по его id."""

        return PROJECT_TYPES.get(project_type_id)

    def get_project_requirements(self, project_type_id):
        """Вернуть список обязательных документов для типа проекта."""

        project_type = self.get_project_type(project_type_id)
        if not project_type:
            return []

        result = []
        for document_code in project_type["required_documents"]:
            result.append(
                {
                    "code": document_code,
                    "title": DOCUMENT_REQUIREMENTS[document_code],
                }
            )
        return result

    def get_missing_documents(self, user_id, project_type_id):
        """Посчитать, каких обязательных документов не хватает пользователю."""

        state = self.get_user_state(user_id)
        provided = state.get("provided_documents", [])
        requirements = self.get_project_requirements(project_type_id)

        result = []
        for requirement in requirements:
            if requirement["code"] not in provided:
                result.append(requirement)

        return result

    def mark_document_as_provided(self, user_id, document_code):
        """Отметить документ как предоставленный пользователем."""

        state = USER_STATES.setdefault(user_id, {})
        provided = state.setdefault("provided_documents", [])

        if document_code not in provided:
            provided.append(document_code)

        return provided

    def get_coauthors(self, user_id):
        """Вернуть соавторов пользователя из его состояния."""

        state = self.get_user_state(user_id)
        coauthor_ids = state.get("coauthors", [])
        return [USERS[user_id] for user_id in coauthor_ids if user_id in USERS]

    def create_task(self, creator_id, assignee_id, title, description):
        """Создать задачу в fake-db и записать это действие в аудит."""

        task = {
            "id": f"task_{len(TASKS) + 1}",
            "creator_id": creator_id,
            "assignee_id": assignee_id,
            "title": title,
            "description": description,
            "status": "created",
        }
        TASKS.append(task)
        self.add_audit_log(creator_id, "task.created", {"task_id": task["id"]})
        return task

    def list_tasks(self):
        """Вернуть все задачи, созданные в fake-db."""

        return TASKS

    def create_email_draft(self, user_id, to, subject, body):
        """Создать черновик письма без фактической отправки."""

        # Письмо только создаётся как черновик. Отправки в MVP нет.
        draft = {
            "id": f"email_draft_{len(EMAIL_DRAFTS) + 1}",
            "user_id": user_id,
            "to": to,
            "subject": subject,
            "body": body,
            "status": "draft",
        }
        EMAIL_DRAFTS.append(draft)
        self.add_audit_log(user_id, "email.draft_created", {"draft_id": draft["id"]})
        return draft

    def list_email_drafts(self):
        """Вернуть все черновики писем из fake-db."""

        return EMAIL_DRAFTS

    def add_audit_log(self, user_id, action, details):
        """Добавить запись аудита и вернуть созданный audit item."""

        log_item = {
            "id": f"audit_{len(AUDIT_LOGS) + 1}",
            "user_id": user_id,
            "action": action,
            "details": details,
        }
        AUDIT_LOGS.append(log_item)
        return log_item

    def list_audit_logs(self, user_id=None):
        """Вернуть аудит по пользователю или весь аудит, если user_id не задан."""

        if user_id is None:
            return AUDIT_LOGS
        return [item for item in AUDIT_LOGS if item["user_id"] == user_id]


# Короткий alias, чтобы дальше можно было писать DBClient().
DBClient = FakeDBClient
