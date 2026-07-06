"""90-day retention policy for audit records (Requirement 8.2).

Installs a `prune_expired_audit_records()` function that deletes only rows
older than 90 days, plus a pg_cron schedule when the extension is available
(falls back to manual/off-cluster invocation otherwise).

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-06
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

PRUNE_FUNCTION = """
CREATE OR REPLACE FUNCTION prune_expired_audit_records() RETURNS void AS $$
BEGIN
    -- Requirement 8.2: retain >= 90 days; delete strictly older rows only.
    DELETE FROM retrieval_records WHERE retrieved_at < NOW() - INTERVAL '90 days';
    DELETE FROM execution_actions WHERE invoked_at < NOW() - INTERVAL '90 days';
    DELETE FROM agent_decisions WHERE recorded_at < NOW() - INTERVAL '90 days';
    DELETE FROM ticket_state_transitions WHERE transitioned_at < NOW() - INTERVAL '90 days'
        AND ticket_id IN (SELECT ticket_id FROM tickets WHERE created_at < NOW() - INTERVAL '90 days');
    DELETE FROM tickets WHERE created_at < NOW() - INTERVAL '90 days'
        AND ticket_id NOT IN (SELECT DISTINCT ticket_id FROM ticket_state_transitions);
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.execute(PRUNE_FUNCTION)
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_cron') THEN
                CREATE EXTENSION IF NOT EXISTS pg_cron;
                PERFORM cron.schedule('prune-audit-records', '0 3 * * *',
                                      'SELECT prune_expired_audit_records()');
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS prune_expired_audit_records()")
