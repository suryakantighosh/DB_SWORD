"""Model-B split-role deploy credentials + canary executed_by_role.

Adds nullable columns so existing rows keep working; deploys fall back to
monitoring credentials and surface an InsufficientPrivilege error when the
monitoring role does not own the target table.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "f2a3b4c5d6e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "database_connections",
        sa.Column("encrypted_deploy_connection_string", sa.Text(), nullable=True),
    )
    op.add_column(
        "database_connections",
        sa.Column("deploy_username", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "canary_runs",
        sa.Column("executed_by_role", sa.String(length=50), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("canary_runs", "executed_by_role")
    op.drop_column("database_connections", "deploy_username")
    op.drop_column("database_connections", "encrypted_deploy_connection_string")
