"""Integration test fixtures: real PostgreSQL 16, RabbitMQ 3.12, Qdrant 1.9.

Runs nightly against real containers (testcontainers-python), not on the PR
unit-test gate. The whole suite is skipped automatically when no Docker
daemon is reachable. ``docker-compose.test.yml`` provides the same stack for
manual runs (set OMNI_IT_* env vars to reuse it instead of testcontainers).
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


DOCKER_AVAILABLE = _docker_available()
EXTERNAL_STACK = bool(os.environ.get("OMNI_IT_DATABASE_URL"))

requires_infra = pytest.mark.skipif(
    not (DOCKER_AVAILABLE or EXTERNAL_STACK),
    reason="integration tests need a Docker daemon or an OMNI_IT_* stack "
    "(docker compose -f docker-compose.test.yml up -d)",
)


@pytest.fixture(scope="session")
def infra_urls():
    """(database_url, rabbitmq_url, qdrant_url) from containers or env."""
    if EXTERNAL_STACK:
        yield (
            os.environ["OMNI_IT_DATABASE_URL"],
            os.environ["OMNI_IT_RABBITMQ_URL"],
            os.environ["OMNI_IT_QDRANT_URL"],
        )
        return

    from testcontainers.postgres import PostgresContainer
    from testcontainers.rabbitmq import RabbitMqContainer
    from testcontainers.qdrant import QdrantContainer

    with PostgresContainer("postgres:16", driver="asyncpg") as postgres, \
            RabbitMqContainer("rabbitmq:3.12-management") as rabbitmq, \
            QdrantContainer("qdrant/qdrant:v1.9.7") as qdrant:
        params = rabbitmq.get_connection_params()
        rabbit_url = f"amqp://guest:guest@{params.host}:{params.port}/"
        qdrant_url = f"http://{qdrant.get_container_host_ip()}:{qdrant.get_exposed_port(6333)}"
        yield postgres.get_connection_url(), rabbit_url, qdrant_url
