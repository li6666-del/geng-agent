"""Add durable PC worker ownership without altering existing jobs or cases."""
from alembic import op

from geng_agent.web.models import WorkerAssignment

revision = "0003_pull_workers"
down_revision = "0002_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    WorkerAssignment.__table__.create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    WorkerAssignment.__table__.drop(op.get_bind(), checkfirst=True)
