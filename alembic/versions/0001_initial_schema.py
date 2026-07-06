"""Initial OmniResolve schema: all audit and state tables.

Retention (Requirement 8.2): tickets and audit tables are retained >= 90 days.
Enforcement is via the scheduled cleanup in 0002 which only prunes rows older
than 90 days — nothing younger is ever deleted.

Revision ID: 0001
Revises:
Create Date: 2026-07-06
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tickets",
        sa.Column("ticket_id", sa.String(36), primary_key=True),
        sa.Column("customer_id", sa.String(128), nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_elapsed_seconds", sa.Numeric(10, 3), nullable=True),
    )

    op.create_table(
        "ticket_state_transitions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "ticket_id", sa.String(36), sa.ForeignKey("tickets.ticket_id"), nullable=False
        ),
        sa.Column("previous_state", sa.String(32), nullable=True),
        sa.Column("new_state", sa.String(32), nullable=False),
        sa.Column("triggered_by", sa.String(64), nullable=False),
        sa.Column(
            "transitioned_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index(
        "ix_ticket_state_transitions_ticket_id", "ticket_state_transitions", ["ticket_id"]
    )

    op.create_table(
        "agent_decisions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "ticket_id", sa.String(36), sa.ForeignKey("tickets.ticket_id"), nullable=False
        ),
        sa.Column("agent", sa.String(64), nullable=False),
        sa.Column("decision_type", sa.String(64), nullable=False),
        sa.Column("input_summary", postgresql.JSONB(), nullable=False),
        sa.Column("output_summary", postgresql.JSONB(), nullable=False),
        sa.Column("confidence_score", sa.Numeric(4, 2), nullable=True),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")
        ),
    )
    op.create_index("ix_agent_decisions_ticket_id", "agent_decisions", ["ticket_id"])

    op.create_table(
        "execution_actions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "ticket_id", sa.String(36), sa.ForeignKey("tickets.ticket_id"), nullable=False
        ),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column("request_body", postgresql.JSONB(), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column("response_body", postgresql.JSONB(), nullable=False),
        sa.Column("invoked_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_execution_actions_ticket_id", "execution_actions", ["ticket_id"])

    op.create_table(
        "policy_documents",
        sa.Column("policy_id", sa.String(36), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("category", sa.String(100), nullable=False),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")
        ),
    )

    op.create_table(
        "retrieval_records",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "ticket_id", sa.String(36), sa.ForeignKey("tickets.ticket_id"), nullable=False
        ),
        sa.Column(
            "policy_id",
            sa.String(36),
            sa.ForeignKey("policy_documents.policy_id"),
            nullable=False,
        ),
        sa.Column("similarity_score", sa.Numeric(5, 4), nullable=False),
        sa.Column(
            "retrieved_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")
        ),
    )
    op.create_index("ix_retrieval_records_ticket_id", "retrieval_records", ["ticket_id"])

    op.create_table(
        "escalation_overflow",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("ticket_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="escalation_pending"),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")
        ),
    )
    op.create_index("ix_escalation_overflow_ticket_id", "escalation_overflow", ["ticket_id"])


def downgrade() -> None:
    for table in (
        "escalation_overflow",
        "retrieval_records",
        "policy_documents",
        "execution_actions",
        "agent_decisions",
        "ticket_state_transitions",
        "tickets",
    ):
        op.drop_table(table)
