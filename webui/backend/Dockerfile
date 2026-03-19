FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY webui/backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && \
    pip install --no-cache-dir curl_cffi

COPY . /app

ENV TOKEN_JSON_DIR=/app/codex_tokens

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "webui.backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
