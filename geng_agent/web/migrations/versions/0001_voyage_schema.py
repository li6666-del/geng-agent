"""Create durable voyage tables.

Revision ID: 0001_voyage_schema
Revises: None
"""
from alembic import op

from geng_agent.web.db import Base

revision = "0001_voyage_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
