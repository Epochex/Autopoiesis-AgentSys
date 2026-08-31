FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app
RUN pip install --no-cache-dir 'confluent-kafka>=2.6,<3'
COPY domains/network_rca/__init__.py domains/network_rca/__init__.py
COPY domains/network_rca/event_detection.py domains/network_rca/event_detection.py
COPY frontend/gateway/__init__.py frontend/gateway/__init__.py
COPY frontend/gateway/ingest/event_pipeline.py frontend/gateway/ingest/event_pipeline.py

USER 65532:65532
ENTRYPOINT ["python", "-m", "frontend.gateway.ingest.event_pipeline"]
