FROM node:20-alpine AS frontend-build

WORKDIR /frontend

COPY webui/frontend/package*.json ./
RUN npm ci

COPY webui/frontend/ ./

ARG VITE_API_BASE=/api
ENV VITE_API_BASE=${VITE_API_BASE}

RUN npm run build

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY webui/backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && \
    pip install --no-cache-dir curl_cffi

COPY . /app
COPY --from=frontend-build /frontend/dist /app/webui/frontend/dist

ENV TOKEN_JSON_DIR=/app/codex_tokens

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "webui.backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
