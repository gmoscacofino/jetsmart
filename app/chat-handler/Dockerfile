# Dockerfile del servicio FastAPI nativo del chat-handler en ECS Fargate.
#
# El código es self-contained en app/chat-handler/ — NO depende de lambda/.
# El build context es la RAÍZ del repo solo para acceder al system prompt en
# terraform/infra/templates/. Construir con:
#   docker build -f app/chat-handler/Dockerfile -t jetsmart-chat-handler .
FROM python:3.12-slim

WORKDIR /app

# Dependencias primero para aprovechar la cache de capas de Docker.
COPY app/chat-handler/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Servicio nativo: lógica de negocio (chat_core) + helpers + servidor FastAPI.
COPY app/chat-handler/chat_core.py app/chat-handler/pricing.py app/chat-handler/pii_tokenizer.py ./
COPY app/chat-handler/server.py ./

# System prompt: chat_core lo lee desde /opt/system_prompt.txt (SYSTEM_PROMPT_PATH).
COPY terraform/infra/templates/system_prompt.tpl /opt/system_prompt.txt

# Los secrets y env vars (ANTHROPIC_SECRET_ARN, PII_TOKEN_SECRET, *_TABLE_NAME,
# SNS_TOPIC_ARN, STEP_FUNCTIONS_ARN, AWS_REGION_VAR, COGNITO_*, etc.) los inyecta
# ECS en runtime — no van en la imagen.

EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
