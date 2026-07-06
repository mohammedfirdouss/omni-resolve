# Build context: repo root (docker-compose sets context: .)
FROM python:3.12-slim AS builder
WORKDIR /app
COPY services/knowledge-agent/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 omni
WORKDIR /app
COPY --from=builder /install /usr/local
COPY shared/ shared/
COPY services/knowledge-agent/knowledge_agent/ knowledge_agent/
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1
USER omni
EXPOSE 8002
CMD ["uvicorn", "knowledge_agent.main:app", "--host", "0.0.0.0", "--port", "8002"]
