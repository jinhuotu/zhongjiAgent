"""user department / phone / last_login_at

Revision ID: 0007_user_profile_fields
Revises: 0006_model_configs
Create Date: 2026-08-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_user_profile_fields"
down_revision: Union[str, None] = "0006_model_configs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("department", sa.String(length=64), nullable=True))
    op.add_column("users", sa.Column("phone", sa.String(length=32), nullable=True))
    op.add_column(
        "users",
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "last_login_at")
    op.drop_column("users", "phone")
    op.drop_column("users", "department")
