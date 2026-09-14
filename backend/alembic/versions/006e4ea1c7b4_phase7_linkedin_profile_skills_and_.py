"""phase7 linkedin profile skills and notes

Revision ID: 006e4ea1c7b4
Revises: 7524cc414385
Create Date: 2026-09-14 02:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '006e4ea1c7b4'
down_revision: Union[str, None] = '7524cc414385'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('profile_skill',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('skill_name', sa.String(length=200), nullable=False),
    sa.Column('order_index', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.add_column('profile_basic', sa.Column('additional_notes', sa.Text(), nullable=True))
    op.add_column('personal_project', sa.Column('company_tag', sa.String(length=300), nullable=True))


def downgrade() -> None:
    op.drop_column('personal_project', 'company_tag')
    op.drop_column('profile_basic', 'additional_notes')
    op.drop_table('profile_skill')
