FROM python:3.11-slim

WORKDIR /workspace

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    SERVE_WEB_INTERFACE=true

# Create non-root runtime user for production container security
RUN groupadd -r shopguard && useradd -r -g shopguard -d /workspace shopguard

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY eval/ ./eval/
RUN mkdir -p /workspace/data && chown -R shopguard:shopguard /workspace

USER shopguard

EXPOSE 8080

CMD ["sh", "-c", "uvicorn app.server:fastapi_app --host 0.0.0.0 --port ${PORT}"]
