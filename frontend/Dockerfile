FROM node:24-bookworm-slim AS frontend-build

WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1
WORKDIR /app/repo

COPY frontend/gateway/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY core /app/repo/core
COPY domains /app/repo/domains
COPY frontend/gateway /app/repo/frontend/gateway
COPY pyproject.toml /app/repo/pyproject.toml
COPY --from=frontend-build /app/frontend/dist /app/frontend/dist

ENV AUTOPOIESIS_REPO_ROOT=/app/repo
ENV AUTOPOIESIS_FRONTEND_DIST=/app/frontend/dist

EXPOSE 8026

CMD ["uvicorn", "frontend.gateway.app.main:app", "--host", "0.0.0.0", "--port", "8026"]
