# University Agent MVP

MVP агентной архитектуры для AI-платформы университета. Проект показывает, как пользовательский запрос проходит через IAM/security слой, агентный граф, MCP-инструменты и отдельный LLM-контур.

Основная идея: AI-агент помогает сотруднику университета подготовить пакет для внутреннего научного проекта, но не получает прямой неограниченный доступ к данным и действиям. Права и доступные документы вычисляет отдельный security service на основе Keycloak, а агент работает только с уже разрешённым контекстом.

## Какую задачу решает проект

Система автоматизирует типовой университетский сценарий:

1. Пользователь авторизуется через Keycloak.
2. Security service проверяет токен, роли и группы пользователя.
3. Security service возвращает агенту список разрешённых действий и документов.
4. LangGraph-агент проходит по сценарию из `agent/skills.txt`.
5. Tool node вызывает следующий MCP tool по имени, передавая ему текущий `state`.
6. Если аргументов или подтверждения не хватает, tool делает interrupt и возвращает вопрос пользователю.
7. MCP tools выполняют поиск знаний, проверку требований, подготовку задач и черновика письма.
8. LLM используется только через отдельный gateway-контур `Envoy -> LiteLLM -> vLLM -> Qwen`.
9. Действия, которые могут изменить состояние, выполняются только с учётом прав и подтверждения пользователя.
10. В конце пишется audit log и возвращается итоговый ответ с источниками.

## Архитектура

```text
User
  |
  v
Security service
  |
  |-- Keycloak IAM
  |-- fake-db: ACL документов, пользователи, состояния, аудит
  |
  v
Agent service
  |
  |-- LangGraph: agent <-> toolnode
  |-- ReAct: выбор следующего MCP tool
  |-- skills.txt: сценарий выполнения
  |
  |-----------------> MCP service
  |                    |
  |                    |-- tools: argument/confirmation interrupts
  |                    |-- tools: search, requirements, tasks, email draft, audit
  |                    |-- fake-db
  |                    |
  |                    v
  |                  Envoy
  |                    |
  |                    v
  |                  LiteLLM
  |                    |
  |                    v
  |                 vLLM / Qwen
```

Ключевое ограничение архитектуры: LLM не является источником прав доступа. Модель получает только тот контекст, который уже разрешён security слоем.

## Технологии

- `Python` - простой MVP-код без тяжёлых фреймворков.
- `Keycloak` - реальный IAM слой.
- `LangGraph` - агентный граф.
- `FastMCP` - MCP слой для инструментов агента.
- `Envoy` - gateway перед LLM-контуром.
- `LiteLLM` - совместимый LLM proxy/router.
- `vLLM` - OpenAI-compatible inference server.
- `Qwen/Qwen2.5-0.5B-Instruct` - маленькая модель для проверки контура.
- `Docker Compose` - локальный запуск сервисов.
- `unittest` - TDD-проверки MVP.

## Структура проекта

```text
fake-db/
  data.py          - фейковые данные: пользователи, документы, требования, состояния
  db_client.py     - простой клиент fake-db

security/
  security_main.py - security service, Keycloak client, authz endpoints
  Dockerfile       - контейнер security service

agent/
  agent.py         - LangGraph agent service
  skills.txt       - бизнес-сценарий, который проходит агент
  requirements.txt - зависимости агента
  Dockerfile       - контейнер agent service

MCP/
  mcp_client.py    - FastMCP tools для бизнес-действий агента
  requirements.txt - зависимости MCP service
  Dockerfile       - контейнер MCP service

vllm-client/
  vllm_usage.py    - клиент MCP tool -> Envoy -> LiteLLM -> vLLM
  Dockerfile       - отдельный контейнер для проверки клиента

infra/
  keycloak/realm-university.json - dev realm, client, роли, группы, тестовый пользователь
  envoy/envoy.yaml               - маршрут Envoy на LiteLLM
  litellm/config.yaml            - маршрут LiteLLM на vLLM/Qwen

tests/
  test_*.py       - unit-тесты ключевых слоёв

scripts/
  smoke_test.sh   - end-to-end проверка через Keycloak, security и agent

docker-compose.yml - локальный контур всех сервисов
README.md          - описание проекта
.gitignore         - исключения для git
```

## Модули

### fake-db

Фейковая база данных для MVP. Хранит прикладные данные: профили пользователей для соавторов, документы, ACL документов по группам, требования к проектам, состояние пользователя, задачи, черновики писем и audit log.

Права пользователя не берутся из fake-db. IAM и действия пользователя считаются в security service на основе Keycloak. В промышленной версии fake-db можно заменить на PostgreSQL, объектное хранилище, векторную БД и реальные интеграции.

### security

Отдельный сервис безопасности. Он:

- проверяет access token через Keycloak;
- получает пользователя, роли и группы;
- считает прикладные права;
- фильтрует документы по группам пользователя;
- возвращает агенту готовый `auth_context`;
- пишет audit log по security-событиям.

Основные endpoints:

```text
GET  /health
POST /auth/context
POST /authz/check
POST /authz/document
POST /authz/filter-documents
```

### agent

Сервис агента. Внутри используется LangGraph с двумя нодами:

```text
agent <-> toolnode
```

`agent` выбирает следующий шаг сценария, а `toolnode` вызывает одноимённый MCP tool. Цикл идёт, пока агент не пройдёт все функции из `skills.txt`.

Агент реализован как простой ReAct-контур:

- `Thought` - `agent` выбирает следующий tool из сценария;
- `Action` - `toolnode` вызывает выбранный MCP tool по имени;
- `Observation` - результат tool сохраняется в state;
- `Interrupt` - сам MCP tool останавливает граф, если ему не хватает аргументов или нужно подтверждение риска.

Важно: в `agent.py` нет бизнес-реализаций tools. Проверка недостающих аргументов и подтверждений живёт в MCP tools. Это соответствует ТЗ: tool сам знает, какие аргументы ему нужны, и сам решает, можно ли продолжать выполнение.

Основной endpoint:

```text
POST /agent/run
```

### MCP

MCP-сервис на FastMCP. Это контрактный слой между агентом и бизнес-инструментами.

Реализованные tools:

- `add_audit_log`
- `auth_context`
- `detect_intent`
- `search_knowledge`
- `check_requirements`
- `list_missing_documents`
- `prepare_application_structure`
- `prepare_tasks`
- `prepare_email_draft`
- `quality_check`
- `write_audit`

### vllm-client

Минимальный OpenAI-compatible клиент. Агент не ходит напрямую в vLLM, а отправляет запрос в gateway:

```text
MCP tool -> Envoy -> LiteLLM -> vLLM(Qwen)
```

Клиент используется MCP tool `prepare_email_draft` для подготовки текста черновика письма.

## Запуск полного контура

Нужны Docker Compose и NVIDIA GPU для vLLM. Даже маленькая Qwen-модель в этом варианте запускается через `vllm/vllm-openai`, поэтому CPU-only машина может не подойти для полного LLM-контура.

```bash
docker compose up --build
```

Первый запуск может занять время: Docker скачает образы, а vLLM скачает модель `Qwen/Qwen2.5-0.5B-Instruct`.

Когда сервисы поднялись, во втором терминале:

```bash
bash scripts/smoke_test.sh
```

Smoke test делает три действия:

1. Получает access token в Keycloak.
2. Проверяет security context через `/auth/context`.
3. Запускает агента через `/agent/run`.

## Важные порты

```text
8081 - Keycloak
8001 - security service
8002 - agent service
8003 - MCP service
8080 - Envoy LLM gateway
4000 - LiteLLM
8000 - vLLM OpenAI API
9901 - Envoy admin
```

## Тестовый пользователь

```text
username: user_ivanov
password: password
client_id: university-agent
realm: university
```

Keycloak admin console:

```text
url: http://localhost:8081
username: admin
password: admin
```

## Ручная проверка

Получить токен:

```bash
curl -s \
  -d "client_id=university-agent" \
  -d "username=user_ivanov" \
  -d "password=password" \
  -d "grant_type=password" \
  http://localhost:8081/realms/university/protocol/openid-connect/token
```

Проверить LLM напрямую через Envoy:

```bash
curl -s http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen",
    "messages": [
      {"role": "user", "content": "Ответь одним предложением: система работает?"}
    ],
    "temperature": 0.2
  }' | python3 -m json.tool
```

Запустить агента вручную можно через `POST http://localhost:8002/agent/run`, передав access token в `Authorization: Bearer ...` или в поле `access_token`.

Пример тела запроса:

```json
{
  "message": "Помоги подготовить пакет для подачи заявки на внутренний научный проект. Найди регламенты, проверь требования, собери список недостающих документов, предложи структуру заявки, подготовь задачи соавторам и черновик письма.",
  "user_arguments": {},
  "confirmations": {}
}
```

Если tool не может выполниться без уточнения, ответ будет содержать:

```json
{
  "waiting_for_user": true,
  "pending_question": {
    "type": "missing_arguments",
    "tool": "search_knowledge",
    "text": "Для продолжения нужно уточнить аргументы: query.",
    "resume_example": {
      "user_arguments": {
        "query": "регламент внутреннего научного проекта"
      }
    }
  }
}
```

Если tool хочет выполнить рискованное действие, ответ будет содержать:

```json
{
  "waiting_for_user": true,
  "requires_confirmation": true,
  "pending_question": {
    "type": "confirmation",
    "tool": "prepare_tasks",
    "text": "Нужно подтверждение перед выполнением рискованного действия: Будут созданы задачи соавторам в системе задач.",
    "resume_example": {
      "confirmations": {
        "prepare_tasks": true
      }
    }
  }
}
```

Для продолжения клиент повторяет `/agent/run` с нужными `user_arguments` или `confirmations`.

Пример подтверждения создания задач:

```json
{
  "message": "Подготовь заявку и создай задачи соавторам",
  "confirmations": {
    "prepare_tasks": true
  }
}
```

Если подтверждения нет, `prepare_tasks` может подготовить безопасный план задач без создания записей. Если пользователь явно просит создать задачи, но `confirmations.prepare_tasks` не передан, tool остановит граф и попросит подтверждение.

## Тесты

Запуск unit-тестов:

```bash
python3 -B -m unittest discover -s tests
```

Проверка Docker Compose конфигурации:

```bash
docker compose config
```

Тестами покрыты:

- fake-db операции;
- security service и проверка прав;
- LangGraph agent flow;
- ReAct interrupts по аргументам и подтверждениям;
- vLLM client;
- MCP tools.

## Переменные окружения

Основные переменные:

```text
KEYCLOAK_URL=http://keycloak:8080
KEYCLOAK_REALM=university
KEYCLOAK_CLIENT_ID=university-agent
KEYCLOAK_CLIENT_SECRET=

SECURITY_URL=http://security:8001
MCP_URL=http://mcp:8003/mcp

LLM_GATEWAY_URL=http://envoy:8080
LLM_MODEL=qwen
LLM_TIMEOUT_SECONDS=60
```

В dev realm используется public client без секрета. Если добавить `KEYCLOAK_CLIENT_SECRET`, security service будет проверять токен через introspection endpoint.

## Что уже соответствует требованиям

- Реальный Keycloak на IAM слое.
- Отдельный security service.
- Отдельный LLM-контур за Envoy.
- LiteLLM между Envoy и vLLM.
- Qwen как LLM-модель.
- LangGraph для агента.
- Граф агента только из двух нод: `agent` и `toolnode`.
- ReAct-поведение через tool-level interrupts.
- Tools сами проверяют недостающие аргументы и необходимость подтверждения.
- MCP слой для инструментов.
- Fake DB только для прикладных данных.
- TDD/unit-тесты для основных слоёв.
- Audit log для действий security и агента.
- Черновик письма без отправки.
- Создание задач только с проверкой прав и подтверждением.

## Ограничения MVP

Это минимальная реализация для демонстрации архитектуры, поэтому в проекте пока нет:

- production-grade обработки ошибок;
- retry/circuit breaker;
- Kubernetes manifests;
- PostgreSQL/Qdrant/MinIO;
- полноценного RAG pipeline с embeddings;
- Prometheus/Grafana/tracing;
- настоящих интеграций с почтой и таск-трекером.

Эти части можно добавлять следующими шагами, не меняя основную архитектурную границу: IAM/security отдельно, агент отдельно, tools через MCP, LLM через gateway.
