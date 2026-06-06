FROM python:3.13-slim

WORKDIR /app

COPY agent/requirements.txt /app/agent/requirements.txt
RUN pip install --no-cache-dir -r /app/agent/requirements.txt

COPY vllm-client /app/vllm-client
COPY agent /app/agent

ENV SECURITY_URL=http://security:8001
ENV LLM_GATEWAY_URL=http://envoy:8080
ENV LLM_MODEL=qwen
ENV MCP_URL=http://mcp:8003/mcp

EXPOSE 8002

CMD ["python", "agent/agent.py"]
