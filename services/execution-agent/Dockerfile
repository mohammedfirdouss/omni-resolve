# Build context: repo root (docker-compose sets context: .)
FROM python:3.12-slim AS builder
WORKDIR /app
COPY services/execution-agent/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 omni
WORKDIR /app
COPY --from=builder /install /usr/local
COPY shared/ shared/
COPY services/execution-agent/execution_agent/ execution_agent/
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1
USER omni
EXPOSE 8003
CMD ["uvicorn", "execution_agent.main:app", "--host", "0.0.0.0", "--port", "8003"]
