FROM python:3.13-slim

WORKDIR /app

COPY fake-db /app/fake-db
COPY security /app/security

ENV KEYCLOAK_URL=http://keycloak:8080
ENV KEYCLOAK_REALM=university
ENV KEYCLOAK_CLIENT_ID=university-agent
ENV KEYCLOAK_TIMEOUT_SECONDS=5

EXPOSE 8001

CMD ["python", "security/security_main.py"]
