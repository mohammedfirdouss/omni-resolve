"""OmniResolve Escalation Agent.

Consumes ``ticket.escalation_requested`` events, idempotently enqueues the
ticket into the human agent queue (keyed on ``ticket_id``), updates the ticket
status to ``escalated``, and publishes ``ticket.escalated``. When the human
queue is unavailable it retains entries in a local retry buffer (max 10,000
entries, retried at 30-second intervals) and emits
``system.escalation_buffer_full`` alerts plus State_Store overflow rows when
the buffer is full.
"""

from escalation_agent.agent import EscalationAgent
from escalation_agent.human_queue import (
    HUMAN_QUEUE_NAME,
    HumanQueue,
    HumanQueueUnavailable,
    InMemoryHumanQueue,
    RabbitMQHumanQueue,
)

__all__ = [
    "EscalationAgent",
    "HUMAN_QUEUE_NAME",
    "HumanQueue",
    "HumanQueueUnavailable",
    "InMemoryHumanQueue",
    "RabbitMQHumanQueue",
]
