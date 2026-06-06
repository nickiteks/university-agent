"""
Клиент для LLM-контура: Agent -> Envoy -> LiteLLM -> vLLM(Qwen).

Название папки vllm-client оставляем как в проекте, но агент не ходит
напрямую в vLLM. Он отправляет OpenAI-compatible запрос в Envoy, а дальше
LiteLLM маршрутизирует запрос в vLLM с моделью Qwen.
"""

import json
import os
from urllib import error, request


class VLLMClient:
    """Минимальный клиент для chat completions."""

    def __init__(self, base_url=None, model=None, api_key=None, timeout=None):
        """Прочитать настройки LLM gateway, модели и таймаута."""

        self.base_url = (base_url or os.getenv("LLM_GATEWAY_URL", "http://envoy:8080")).rstrip("/")
        self.model = model or os.getenv("LLM_MODEL", "qwen")
        self.api_key = api_key or os.getenv("LLM_API_KEY", "")
        self.timeout = timeout or float(os.getenv("LLM_TIMEOUT_SECONDS", "20"))

    def complete(self, system_prompt, user_prompt):
        """Собрать system/user messages и получить текстовый ответ модели."""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        return self.chat(messages)

    def chat(self, messages, temperature=0.2):
        """Отправить chat completion запрос и вернуть текст ассистента."""

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        data = self.send_payload(payload)
        return self.extract_text(data)

    def send_payload(self, payload):
        """Отправить OpenAI-compatible payload в Envoy/LiteLLM gateway."""

        url = self.base_url + "/v1/chat/completions"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}

        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        http_request = request.Request(url, data=body, headers=headers, method="POST")

        try:
            with request.urlopen(http_request, timeout=self.timeout) as response:
                raw_data = response.read().decode("utf-8")
                return json.loads(raw_data)
        except (error.HTTPError, error.URLError, TimeoutError, json.JSONDecodeError):
            return {}

    def extract_text(self, data):
        """Достать текст ассистента из ответа chat completions."""

        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return ""


if __name__ == "__main__":
    client = VLLMClient()
    answer = client.complete(
        "Ты помощник университета.",
        "Коротко ответь, что LLM-контур доступен.",
    )
    print(answer)
