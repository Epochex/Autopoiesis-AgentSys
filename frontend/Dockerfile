FROM node:24-bookworm-slim AS frontend-build

WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY frontend/gateway/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY frontend/gateway /app/gateway
COPY --from=frontend-build /app/frontend/dist /app/frontend/dist
COPY core/deployments /app/repo/core/deployments
COPY edge/edge_forwarder/deployments /app/repo/edge/edge_forwarder/deployments

ENV CONTEXTHELIX_REPO_ROOT=/app/repo
ENV CONTEXTHELIX_FRONTEND_DIST=/app/frontend/dist
ENV CONTEXTHELIX_ARTIFACT_ROOT=/data/netops-runtime

EXPOSE 8026

CMD ["uvicorn", "gateway.app.main:app", "--host", "0.0.0.0", "--port", "8026"]
