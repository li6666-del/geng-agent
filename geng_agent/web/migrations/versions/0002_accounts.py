"""Add accounts without deleting historical local case or event data."""
from alembic import op

from geng_agent.web.models import LoginAttempt, SessionRecord, UserRecord

revision = "0002_accounts"
down_revision = "0001_voyage_schema"


def upgrade() -> None:
    for model in (UserRecord, SessionRecord, LoginAttempt):
        model.__table__.create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    for model in (LoginAttempt, SessionRecord, UserRecord):
        model.__table__.drop(op.get_bind(), checkfirst=True)
