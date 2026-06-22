FROM python:3.12-slim

WORKDIR /app

# Instalar dependencias primero para aprovechar la cache de capas de Docker.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY poller.py .

# -u => stdout sin buffer, así awslogs captura los logs en tiempo real.
CMD ["python", "-u", "poller.py"]
