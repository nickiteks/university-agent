import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
VLLM_CLIENT_DIR = ROOT_DIR / "vllm-client"

sys.path.append(str(VLLM_CLIENT_DIR))

from vllm_usage import VLLMClient  # noqa: E402


class TestableVLLMClient(VLLMClient):
    def __init__(self):
        super().__init__(base_url="http://envoy:8080", model="qwen", timeout=1)
        self.last_payload = None

    def send_payload(self, payload):
        self.last_payload = payload
        return {
            "choices": [
                {
                    "message": {
                        "content": "Черновик ответа от Qwen",
                    }
                }
            ]
        }


class VLLMClientTest(unittest.TestCase):
    def test_complete_uses_qwen_model_and_chat_format(self):
        client = TestableVLLMClient()

        answer = client.complete("Системная инструкция", "Запрос пользователя")

        self.assertEqual("Черновик ответа от Qwen", answer)
        self.assertEqual("qwen", client.last_payload["model"])
        self.assertEqual("system", client.last_payload["messages"][0]["role"])
        self.assertEqual("user", client.last_payload["messages"][1]["role"])

    def test_extract_text_returns_empty_string_for_bad_response(self):
        client = VLLMClient(base_url="http://envoy:8080", model="qwen")

        self.assertEqual("", client.extract_text({}))


if __name__ == "__main__":
    unittest.main()
