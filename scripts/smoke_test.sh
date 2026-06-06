#!/usr/bin/env bash
set -euo pipefail

TOKEN_RESPONSE="$(curl -s \
  -d "client_id=university-agent" \
  -d "username=user_ivanov" \
  -d "password=password" \
  -d "grant_type=password" \
  http://localhost:8081/realms/university/protocol/openid-connect/token)"

TOKEN="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])' <<< "$TOKEN_RESPONSE")"

echo "Token received from Keycloak"

echo
echo "Security auth context:"
curl -s http://localhost:8001/auth/context \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d "{\"access_token\":\"$TOKEN\"}" \
  | python3 -m json.tool

echo
echo "Agent response:"
curl -s http://localhost:8002/agent/run \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "message": "Помоги подготовить пакет для подачи заявки на внутренний научный проект. Найди регламенты, проверь требования, собери список недостающих документов, предложи структуру заявки, подготовь задачи соавторам и черновик письма.",
    "confirmations": {}
  }' \
  | python3 -m json.tool
