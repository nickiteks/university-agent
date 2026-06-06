FROM python:3.13-slim

WORKDIR /app

COPY vllm-client /app/vllm-client

ENV LLM_GATEWAY_URL=http://envoy:8080
ENV LLM_MODEL=qwen
ENV LLM_TIMEOUT_SECONDS=20

CMD ["python", "vllm-client/vllm_usage.py"]
