"""
MCP-сервис для бизнес-инструментов агента.

Сервис разворачивается на FastMCP и содержит @tool функции.
Агент вызывает эти tools из своей LangGraph-ноды toolnode.

В MVP tools работают с fake-db. В промышленной версии вместо fake-db здесь
будут MCP-адаптеры к трекеру задач, почте, хранилищам и другим системам.
"""

import os
from pathlib import Path
import sys

from fastmcp import FastMCP


PROJECT_DIR = Path(__file__).resolve().parents[1]
FAKE_DB_DIR = PROJECT_DIR / "fake-db"
VLLM_CLIENT_DIR = PROJECT_DIR / "vllm-client"

if str(FAKE_DB_DIR) not in sys.path:
    sys.path.append(str(FAKE_DB_DIR))

if str(VLLM_CLIENT_DIR) not in sys.path:
    sys.path.append(str(VLLM_CLIENT_DIR))

from db_client import DBClient  # noqa: E402
from vllm_usage import VLLMClient  # noqa: E402


mcp = FastMCP("university-agent-mcp")
db = DBClient()
llm_client = None


def get_llm_client():
    """Вернуть LLM-клиент для генерации текста.

    Принимаемые аргументы:
    - нет.

    Возвращаемое значение:
    - экземпляр VLLMClient, который ходит в LLM gateway
      Envoy -> LiteLLM -> vLLM/Qwen.
    """

    global llm_client

    if llm_client is None:
        llm_client = VLLMClient()

    return llm_client


def build_argument_question(tool_name, missing_arguments):
    """Собрать вопрос пользователю по недостающим аргументам tool.

    Принимаемые аргументы:
    - tool_name: имя MCP tool, который не может продолжить работу.
    - missing_arguments: список dict с полями name, description, example.

    Возвращаемое значение:
    - dict pending_question для ответа агенту и UI.
    """

    argument_names = [item["name"] for item in missing_arguments]

    return {
        "type": "missing_arguments",
        "tool": tool_name,
        "text": (
            "Для продолжения нужно уточнить аргументы: "
            + ", ".join(argument_names)
            + "."
        ),
        "required_arguments": missing_arguments,
        "resume_example": {
            "user_arguments": {
                item["name"]: item["example"] for item in missing_arguments
            }
        },
    }


def interrupt_for_arguments(tool_name, missing_arguments):
    """Вернуть interrupt, если tool не хватает аргументов.

    Принимаемые аргументы:
    - tool_name: имя MCP tool.
    - missing_arguments: список недостающих аргументов.

    Возвращаемое значение:
    - dict с waiting_for_user=True, pending_question, final_answer,
      stop=True и react_trace. Агент сольёт эти поля в state и остановит граф.
    """

    question = build_argument_question(tool_name, missing_arguments)

    return {
        "waiting_for_user": True,
        "requires_confirmation": False,
        "missing_arguments": missing_arguments,
        "pending_question": question,
        "final_answer": question["text"],
        "stop": True,
        "react_trace": [
            {
                "thought": "MCP tool не может выполниться без обязательных аргументов.",
                "tool": tool_name,
                "action": "interrupt",
                "question": question,
            }
        ],
    }


def build_confirmation_question(pending_confirmation):
    """Собрать вопрос пользователю для подтверждения рискованного tool.

    Принимаемые аргументы:
    - pending_confirmation: dict с полями tool, risk, required_confirmation.

    Возвращаемое значение:
    - dict pending_question с типом confirmation и примером resume payload.
    """

    tool_name = pending_confirmation["tool"]

    return {
        "type": "confirmation",
        "tool": tool_name,
        "text": (
            "Нужно подтверждение перед выполнением рискованного действия: "
            + pending_confirmation["risk"]
        ),
        "risk": pending_confirmation["risk"],
        "resume_example": {
            "confirmations": {
                tool_name: True,
            }
        },
    }


def interrupt_for_confirmation(pending_confirmation):
    """Вернуть interrupt, если tool требует подтверждения пользователя.

    Принимаемые аргументы:
    - pending_confirmation: описание рискованного действия.

    Возвращаемое значение:
    - dict с waiting_for_user=True, requires_confirmation=True,
      pending_question, final_answer, stop=True и react_trace.
    """

    question = build_confirmation_question(pending_confirmation)

    return {
        "waiting_for_user": True,
        "requires_confirmation": True,
        "pending_confirmation": pending_confirmation,
        "pending_question": question,
        "final_answer": question["text"],
        "stop": True,
        "react_trace": [
            {
                "thought": "MCP tool остановлен до подтверждения рискованного действия.",
                "tool": pending_confirmation["tool"],
                "action": "interrupt",
                "question": question,
            }
        ],
    }


def is_tool_confirmed(state, tool_name):
    """Проверить точечное подтверждение tool от пользователя.

    Принимаемые аргументы:
    - state: текущее состояние графа агента.
    - tool_name: имя tool, для которого ищем подтверждение.

    Возвращаемое значение:
    - bool: True, если state.confirmations содержит подтверждение tool.
    """

    confirmations = state.get("confirmations", {})
    return bool(confirmations.get(tool_name))


def wants_to_create_tasks(state):
    """Понять, хочет ли пользователь именно создать задачи.

    Принимаемые аргументы:
    - state: текущее состояние графа агента, используется поле message.

    Возвращаемое значение:
    - bool: True, если текст запроса похож на команду создать задачи.
    """

    message = state.get("message", "").lower()
    create_words = [
        "создай",
        "создать",
        "создайте",
        "заведи",
        "завести",
        "поставь задачи",
        "создание задач",
    ]

    for word in create_words:
        if word in message:
            return True

    return False


def build_email_body(state):
    """Сформировать текст письма через LLM-контур или простой fallback.

    Принимаемые аргументы:
    - state: текущее состояние графа агента. Используются поля
      missing_documents и sources.

    Возвращаемое значение:
    - строка body для черновика письма.
    """

    missing_titles = [item["title"] for item in state.get("missing_documents", [])]
    source_titles = [source["title"] for source in state.get("sources", [])]

    system_prompt = (
        "Ты помогаешь сотруднику университета. "
        "Подготовь только черновик письма, ничего не отправляй."
    )
    user_prompt = (
        f"Недостающие документы: {', '.join(missing_titles)}\n"
        f"Источники: {', '.join(source_titles)}"
    )

    llm_text = get_llm_client().complete(system_prompt, user_prompt)
    if llm_text:
        return llm_text

    return (
        "Коллеги, добрый день.\n\n"
        "Прошу согласовать подготовку заявки на внутренний научный проект. "
        "По текущей проверке нужно дополнить пакет документами: "
        f"{', '.join(missing_titles)}.\n\n"
        "После согласования обновлю комплект материалов."
    )


@mcp.tool
def add_audit_log(user_id: str, action: str, details: dict) -> dict:
    """Записать действие агента в аудит.

    Принимаемые аргументы:
    - user_id: id пользователя, от имени которого пишется событие.
    - action: техническое имя события.
    - details: произвольные детали события.

    Возвращаемое значение:
    - dict audit log item с id, user_id, action и details.
    """

    return db.add_audit_log(user_id, action, details)


@mcp.tool
def auth_context(state: dict) -> dict:
    """Проверить auth context из security слоя и записать стартовый аудит.

    Принимаемые аргументы:
    - state: состояние графа. Обязательные поля:
      auth_context, message.

    Возвращаемое значение:
    - при успешной авторизации: {"user_id": "..."}.
    - при ошибке авторизации: поля stop, final_answer, errors.
    """

    context = state.get("auth_context", {})
    if not context.get("authenticated"):
        return {
            "stop": True,
            "final_answer": "Нет доступа: токен не прошёл проверку Keycloak.",
            "errors": [context.get("error", "invalid_token")],
        }

    user_id = context["user"]["id"]
    db.add_audit_log(
        user_id,
        "agent.request_started",
        {
            "message": state.get("message", ""),
        },
    )

    return {
        "user_id": user_id,
    }


@mcp.tool
def detect_intent(state: dict) -> dict:
    """Определить намерение и тип проекта для текущего MVP-сценария.

    Принимаемые аргументы:
    - state: состояние графа. Используются user_id, user_arguments,
      scenario_tools.

    Возвращаемое значение:
    - intent: техническое имя намерения.
    - project_type: тип проекта, например internal_research.
    - plan: список шагов сценария из skills.txt.
    """

    user_id = state["user_id"]
    user_arguments = state.get("user_arguments", {})
    scenario_descriptions = [
        tool["description"] for tool in state.get("scenario_tools", [])
    ]
    user_state = db.get_user_state(user_id)

    return {
        "intent": "prepare_internal_research_application",
        "project_type": (
            user_arguments.get("project_type")
            or user_state.get("current_project_type", "internal_research")
        ),
        "plan": scenario_descriptions,
    }


@mcp.tool
def search_knowledge(state: dict) -> dict:
    """Найти источники знаний среди документов, разрешённых security слоем.

    Принимаемые аргументы:
    - state: состояние графа. Используются auth_context.allowed_document_ids,
      user_arguments.query и message.

    Возвращаемое значение:
    - при успехе: {"sources": [...]}.
      Каждый source содержит id, title, version, source_url.
    - если нет query: interrupt с pending_question типа missing_arguments.
    """

    auth_context = state["auth_context"]
    user_arguments = state.get("user_arguments", {})
    user_query = user_arguments.get("query") or state.get("message", "")
    if not user_query:
        missing_arguments = [
            {
                "name": "query",
                "description": "Поисковый запрос для документов и регламентов.",
                "example": "регламент внутреннего научного проекта",
            }
        ]
        return interrupt_for_arguments("search_knowledge", missing_arguments)

    allowed_document_ids = auth_context["allowed_document_ids"]
    query = user_query + " регламент шаблон инструкция заявка бюджет требования"
    documents = db.search_documents_by_ids(allowed_document_ids, query)
    sources = []

    for document in documents:
        sources.append(
            {
                "id": document["id"],
                "title": document["title"],
                "version": document["version"],
                "source_url": document["source_url"],
            }
        )

    return {
        "sources": sources,
    }


@mcp.tool
def check_requirements(state: dict) -> dict:
    """Получить требования к документам для типа проекта.

    Принимаемые аргументы:
    - state: состояние графа. Используются user_arguments.project_type
      или уже определённый state.project_type.

    Возвращаемое значение:
    - project_type: выбранный тип проекта.
    - project_requirements: список обязательных документов.
    - если нет project_type: interrupt с pending_question типа missing_arguments.
    """

    project_type = state.get("user_arguments", {}).get("project_type") or state.get("project_type")
    if not project_type:
        missing_arguments = [
            {
                "name": "project_type",
                "description": "Тип проекта, для которого нужно проверить требования.",
                "example": "internal_research",
            }
        ]
        return interrupt_for_arguments("check_requirements", missing_arguments)

    return {
        "project_type": project_type,
        "project_requirements": db.get_project_requirements(project_type),
    }


@mcp.tool
def list_missing_documents(state: dict) -> dict:
    """Составить список документов, которых не хватает пользователю.

    Принимаемые аргументы:
    - state: состояние графа. Используются user_id, user_arguments.project_type
      или state.project_type.

    Возвращаемое значение:
    - project_type: выбранный тип проекта.
    - missing_documents: список недостающих документов с code и title.
    - если нет project_type: interrupt с pending_question типа missing_arguments.
    """

    user_id = state["user_id"]
    project_type = state.get("user_arguments", {}).get("project_type") or state.get("project_type")
    if not project_type:
        missing_arguments = [
            {
                "name": "project_type",
                "description": "Тип проекта, для которого нужно проверить требования.",
                "example": "internal_research",
            }
        ]
        return interrupt_for_arguments("list_missing_documents", missing_arguments)

    return {
        "project_type": project_type,
        "missing_documents": db.get_missing_documents(user_id, project_type),
    }


@mcp.tool
def prepare_application_structure(state: dict) -> dict:
    """Подготовить структуру заявки на внутренний научный проект.

    Принимаемые аргументы:
    - state: состояние графа. Для MVP содержимое state не используется.

    Возвращаемое значение:
    - application_structure: список разделов заявки.
    """

    return {
        "application_structure": [
            "Цель проекта",
            "Научная новизна",
            "Команда и роли",
            "План работ",
            "Бюджет",
            "Ожидаемые результаты",
            "Приложения и подтверждающие документы",
        ]
    }


@mcp.tool
def prepare_tasks(state: dict) -> dict:
    """Подготовить или создать задачи соавторам.

    Принимаемые аргументы:
    - state: состояние графа. Используются user_id, message,
      missing_documents, confirmations.prepare_tasks,
      auth_context.agent_context.allowed_actions.

    Возвращаемое значение:
    - если нет права tasks.create: {"errors": [...]}.
    - если пользователь просит создать задачи без подтверждения:
      interrupt с pending_question типа confirmation.
    - если confirmed=False: planned_tasks, created_tasks=[],
      requires_confirmation=True.
    - если confirmed=True: created_tasks, planned_tasks=[],
      requires_confirmation=False.
    """

    allowed_actions = state["auth_context"]["agent_context"]["allowed_actions"]
    if "tasks.create" not in allowed_actions:
        return {
            "errors": ["Нет права tasks.create для создания задач."],
        }

    user_id = state["user_id"]
    missing_documents = state.get("missing_documents", [])
    confirmed = is_tool_confirmed(state, "prepare_tasks")
    wants_create = wants_to_create_tasks(state)

    if wants_create and missing_documents and not confirmed:
        pending_confirmation = {
            "tool": "prepare_tasks",
            "risk": "Будут созданы задачи соавторам в системе задач.",
            "required_confirmation": "confirmations.prepare_tasks = true",
        }
        return interrupt_for_confirmation(pending_confirmation)

    missing_titles = [item["title"] for item in missing_documents]
    if not missing_titles:
        return {
            "planned_tasks": [],
            "created_tasks": [],
            "requires_confirmation": False,
        }

    coauthors = db.get_coauthors(user_id)
    description = "Нужно подготовить: " + ", ".join(missing_titles)

    planned_tasks = []
    created_tasks = []
    requires_confirmation = False

    for coauthor in coauthors:
        task_data = {
            "assignee_id": coauthor["id"],
            "title": "Подготовить документы для заявки",
            "description": description,
        }

        if confirmed:
            task = db.create_task(
                user_id,
                coauthor["id"],
                task_data["title"],
                task_data["description"],
            )
            created_tasks.append(task)
        else:
            planned_tasks.append(task_data)
            requires_confirmation = True

    return {
        "planned_tasks": planned_tasks,
        "created_tasks": created_tasks,
        "requires_confirmation": requires_confirmation,
    }


@mcp.tool
def prepare_email_draft(state: dict) -> dict:
    """Создать черновик письма без отправки.

    Принимаемые аргументы:
    - state: состояние графа. Используются user_id, missing_documents,
      sources, auth_context.agent_context.allowed_actions.

    Возвращаемое значение:
    - при успехе: {"email_draft": {...}} со status="draft".
    - если нет права email.draft: {"errors": [...]}.
    """

    allowed_actions = state["auth_context"]["agent_context"]["allowed_actions"]
    if "email.draft" not in allowed_actions:
        return {
            "errors": ["Нет права email.draft для создания черновика письма."],
        }

    user_id = state["user_id"]
    coauthors = db.get_coauthors(user_id)
    recipients = [coauthor["email"] for coauthor in coauthors]
    body = build_email_body(state)

    draft = db.create_email_draft(
        user_id,
        recipients,
        "Согласование заявки на внутренний научный проект",
        body,
    )

    return {
        "email_draft": draft,
    }


@mcp.tool
def quality_check(state: dict) -> dict:
    """Проверить качество результата и доступность источников.

    Принимаемые аргументы:
    - state: состояние графа. Используются sources, email_draft,
      auth_context.allowed_document_ids.

    Возвращаемое значение:
    - quality: dict с флагами has_sources, email_is_draft,
      uses_allowed_documents_only.
    - errors: список найденных проблем.
    """

    sources = state.get("sources", [])
    email_draft = state.get("email_draft", {})
    allowed_document_ids = state["auth_context"]["allowed_document_ids"]
    source_ids = [source["id"] for source in sources]
    uses_allowed_documents_only = True

    for source_id in source_ids:
        if source_id not in allowed_document_ids:
            uses_allowed_documents_only = False

    quality = {
        "has_sources": bool(sources),
        "email_is_draft": email_draft.get("status") == "draft",
        "uses_allowed_documents_only": uses_allowed_documents_only,
    }

    errors = []
    if not quality["has_sources"]:
        errors.append("Не найдены источники для ответа.")

    if not quality["uses_allowed_documents_only"]:
        errors.append("Ответ содержит источник без доступа пользователя.")

    return {
        "quality": quality,
        "errors": errors,
    }


@mcp.tool
def write_audit(state: dict) -> dict:
    """Записать финальный аудит и собрать итоговый ответ пользователю.

    Принимаемые аргументы:
    - state: состояние графа. Используются user_id, completed_tools,
      sources, missing_documents, application_structure, planned_tasks,
      created_tasks, email_draft, requires_confirmation.

    Возвращаемое значение:
    - final_answer: человекочитаемый итоговый текст.
    - audit_log: запись аудита agent.request_finished.
    """

    user_id = state["user_id"]
    completed_tools = state.get("completed_tools", [])
    sources = state.get("sources", [])
    missing_documents = state.get("missing_documents", [])
    application_structure = state.get("application_structure", [])
    planned_tasks = state.get("planned_tasks", [])
    created_tasks = state.get("created_tasks", [])
    email_draft = state.get("email_draft", {})
    requires_confirmation = state.get("requires_confirmation", False)

    final_answer = build_final_answer(
        sources,
        missing_documents,
        application_structure,
        planned_tasks,
        created_tasks,
        email_draft,
        requires_confirmation,
    )

    audit_log = db.add_audit_log(
        user_id,
        "agent.request_finished",
        {
            "steps": completed_tools,
            "source_count": len(sources),
            "missing_count": len(missing_documents),
        },
    )

    return {
        "final_answer": final_answer,
        "audit_log": audit_log,
    }


def build_final_answer(
    sources,
    missing_documents,
    application_structure,
    planned_tasks,
    created_tasks,
    email_draft,
    requires_confirmation,
):
    """Собрать итоговый текст ответа пользователю.

    Принимаемые аргументы:
    - sources: список источников.
    - missing_documents: список недостающих документов.
    - application_structure: разделы заявки.
    - planned_tasks: задачи, подготовленные без создания.
    - created_tasks: задачи, созданные после подтверждения.
    - email_draft: созданный черновик письма.
    - requires_confirmation: нужен ли пользовательский approve.

    Возвращаемое значение:
    - строка финального ответа для пользователя.
    """

    lines = []
    lines.append("Пакет для внутреннего научного проекта разобран.")
    lines.append("")
    lines.append("Недостающие документы:")
    for document in missing_documents:
        lines.append(f"- {document['title']}")

    lines.append("")
    lines.append("Предлагаемая структура заявки:")
    for item in application_structure:
        lines.append(f"- {item}")

    lines.append("")
    lines.append("Источники:")
    for source in sources:
        lines.append(f"- {source['title']} v{source['version']}: {source['source_url']}")

    if planned_tasks:
        lines.append("")
        lines.append("Задачи соавторам подготовлены, но не созданы без подтверждения.")

    if created_tasks:
        lines.append("")
        lines.append(f"Создано задач: {len(created_tasks)}.")

    if email_draft:
        lines.append("")
        lines.append(f"Черновик письма подготовлен: {email_draft['id']}.")

    if requires_confirmation:
        lines.append("")
        lines.append("Для создания задач нужно подтверждение пользователя.")

    return "\n".join(lines)


def run_server():
    """Запустить FastMCP-сервер с транспортом из переменных окружения.

    Принимаемые аргументы:
    - нет. Настройки берутся из MCP_HOST, MCP_PORT, MCP_TRANSPORT.

    Возвращаемое значение:
    - нет, функция блокирует поток и обслуживает MCP-сервер.
    """

    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8003"))
    transport = os.getenv("MCP_TRANSPORT", "http")

    mcp.run(transport=transport, host=host, port=port)


if __name__ == "__main__":
    run_server()
