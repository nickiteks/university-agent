FROM python:3.13-slim

WORKDIR /app

COPY MCP/requirements.txt /app/MCP/requirements.txt
RUN pip install --no-cache-dir -r /app/MCP/requirements.txt

COPY fake-db /app/fake-db
COPY MCP /app/MCP

ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8003
ENV MCP_TRANSPORT=http

EXPOSE 8003

CMD ["python", "MCP/mcp_client.py"]
