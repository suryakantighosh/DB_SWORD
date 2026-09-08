"""Arc A: add explain_before / explain_after JSON columns to optimization_experiments.

Populated by shadow_lab_worker's EXPLAIN capture around the candidate
install; consumed by the experiment-detail EXPLAIN diff card on the
frontend. Nullable so historical rows and rows produced by paths that
don't run shadow-pool stay valid.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "optimization_experiments",
        sa.Column("explain_before", sa.JSON(), nullable=True),
    )
    op.add_column(
        "optimization_experiments",
        sa.Column("explain_after", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("optimization_experiments", "explain_after")
    op.drop_column("optimization_experiments", "explain_before")
