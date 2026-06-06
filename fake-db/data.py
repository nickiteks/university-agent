"""
Фейковая база данных для MVP.

Тут нет настоящей БД, миграций и сложных моделей.
Это обычные словари и списки, чтобы следующие слои системы могли
работать с понятными сущностями: пользователи, документы, состояние,
задачи, письма и журнал действий.
"""


# Пользователи для прикладных связей MVP: соавторы, email, кафедра.
# Роли и права в рабочем контуре приходят из Keycloak, а не из fake-db.
USERS = {
    "user_ivanov": {
        "id": "user_ivanov",
        "full_name": "Иванов Иван Петрович",
        "email": "ivanov@university.local",
        "department": "Кафедра прикладной информатики",
        "roles": ["employee", "researcher"],
        "groups": ["researchers", "grant_applicants", "ai_lab"],
    },
    "user_petrova": {
        "id": "user_petrova",
        "full_name": "Петрова Анна Сергеевна",
        "email": "petrova@university.local",
        "department": "Проектный офис",
        "roles": ["employee", "grant_officer"],
        "groups": ["grant_office", "researchers"],
    },
    "user_sidorov": {
        "id": "user_sidorov",
        "full_name": "Сидоров Олег Викторович",
        "email": "sidorov@university.local",
        "department": "Кафедра математики",
        "roles": ["employee", "coauthor"],
        "groups": ["researchers"],
    },
    "user_guest": {
        "id": "user_guest",
        "full_name": "Гостевой пользователь",
        "email": "guest@university.local",
        "department": "Внешний доступ",
        "roles": ["guest"],
        "groups": ["guests"],
    },
}


# Документы RAG-слоя. MinIO хранил бы файл, Qdrant - эмбеддинги,
# PostgreSQL - метаданные. Для MVP держим всё рядом.
DOCUMENTS = {
    "doc_reg_internal_2026": {
        "id": "doc_reg_internal_2026",
        "title": "Регламент внутренних научных проектов 2026",
        "document_type": "regulation",
        "version": "2026.1",
        "status": "active",
        "project_types": ["internal_research"],
        "access_groups": ["researchers", "grant_office"],
        "storage_path": "minio://regulations/internal-projects-2026.pdf",
        "source_url": "kb://documents/doc_reg_internal_2026",
        "keywords": ["регламент", "научный проект", "внутренний проект"],
        "content": (
            "Для внутреннего научного проекта нужны заявка, бюджет, "
            "состав команды, календарный план и согласие руководителя."
        ),
    },
    "doc_template_application": {
        "id": "doc_template_application",
        "title": "Шаблон заявки на внутренний научный проект",
        "document_type": "template",
        "version": "2026.1",
        "status": "active",
        "project_types": ["internal_research"],
        "access_groups": ["researchers", "grant_office"],
        "storage_path": "minio://templates/internal-project-application.docx",
        "source_url": "kb://documents/doc_template_application",
        "keywords": ["шаблон", "заявка", "структура заявки"],
        "content": (
            "Структура заявки: цель проекта, научная новизна, команда, "
            "план работ, бюджет, ожидаемые результаты."
        ),
    },
    "doc_instruction_budget": {
        "id": "doc_instruction_budget",
        "title": "Инструкция по подготовке бюджета проекта",
        "document_type": "instruction",
        "version": "2025.4",
        "status": "active",
        "project_types": ["internal_research"],
        "access_groups": ["researchers", "grant_office"],
        "storage_path": "minio://instructions/project-budget.pdf",
        "source_url": "kb://documents/doc_instruction_budget",
        "keywords": ["бюджет", "смета", "финансирование"],
        "content": (
            "Бюджет должен содержать статьи расходов, обоснование, "
            "помесячный план и итоговую сумму."
        ),
    },
    "doc_private_grant_office": {
        "id": "doc_private_grant_office",
        "title": "Внутренняя памятка проектного офиса",
        "document_type": "instruction",
        "version": "2026.1",
        "status": "active",
        "project_types": ["internal_research"],
        "access_groups": ["grant_office"],
        "storage_path": "minio://private/grant-office-note.pdf",
        "source_url": "kb://documents/doc_private_grant_office",
        "keywords": ["проектный офис", "служебная памятка"],
        "content": "Служебные правила проверки заявок проектным офисом.",
    },
}


# Человекочитаемые названия документов, которые пользователь должен собрать.
DOCUMENT_REQUIREMENTS = {
    "application_form": "Заполненная заявка",
    "project_budget": "Бюджет проекта",
    "team_list": "Состав команды",
    "calendar_plan": "Календарный план",
    "manager_approval": "Согласие руководителя",
}


# Требования зависят от типа проекта.
PROJECT_TYPES = {
    "internal_research": {
        "id": "internal_research",
        "title": "Внутренний научный проект",
        "required_documents": [
            "application_form",
            "project_budget",
            "team_list",
            "calendar_plan",
            "manager_approval",
        ],
        "source_document_ids": [
            "doc_reg_internal_2026",
            "doc_template_application",
            "doc_instruction_budget",
        ],
    }
}


# Состояние диалога/работы пользователя.
USER_STATES = {
    "user_ivanov": {
        "current_project_type": "internal_research",
        "provided_documents": ["application_form", "team_list"],
        "coauthors": ["user_sidorov"],
        "last_intent": "prepare_internal_research_application",
    }
}


# Ниже имитируем таблицы с изменяемыми данными.
TASKS = []
EMAIL_DRAFTS = []
AUDIT_LOGS = []
