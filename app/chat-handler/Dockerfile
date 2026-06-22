# Dockerfile para el adaptador FastAPI que ejecuta chat_handler.py en ECS Fargate.
#
# IMPORTANTE: el build context es la RAÍZ del repo (tps/entrega-tp3/jetsmart/),
# no este directorio, porque copiamos archivos de lambda/ y terraform/.
# Construir con:
#   docker build -f app/chat-handler/Dockerfile -t jetsmart-chat-handler .
FROM python:3.12-slim

WORKDIR /app

# Dependencias primero para aprovechar la cache de capas de Docker.
COPY app/chat-handler/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Código del handler existente (lógica de negocio) — reusado tal cual, sin reescribir.
COPY lambda/chat_handler.py lambda/pricing.py lambda/pii_tokenizer.py ./

# System prompt: el handler lo lee desde /opt/system_prompt.txt (SYSTEM_PROMPT_PATH).
COPY terraform/infra/templates/system_prompt.tpl /opt/system_prompt.txt

# Adaptador FastAPI.
COPY app/chat-handler/server.py ./

# Los secrets y env vars (ANTHROPIC_SECRET_ARN, PII_TOKEN_SECRET, *_TABLE_NAME,
# SNS_TOPIC_ARN, STEP_FUNCTIONS_ARN, AWS_REGION_VAR, COGNITO_*, etc.) los inyecta
# ECS en runtime — no van en la imagen.

EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
