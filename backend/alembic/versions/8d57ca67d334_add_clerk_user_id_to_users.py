"""add clerk_user_id to users

Revision ID: 8d57ca67d334
Revises: a1b2c3d4e5f6
Create Date: 2026-07-09 10:25:29.302804

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8d57ca67d334'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Reviewed/adjusted (README step 3): autogenerate emitted an unnamed unique
    # constraint (`create_unique_constraint(None, ...)`), which Postgres names
    # implicitly on create but which alembic can't target on `downgrade()` without
    # a name — named explicitly here, matching Postgres's own default naming
    # convention (`<table>_<column>_key`) so it stays consistent with the
    # pre-existing, equally-unnamed `users_email_key` constraint from the baseline.
    op.add_column('users', sa.Column('clerk_user_id', sa.Text(), nullable=True))
    op.alter_column('users', 'email',
               existing_type=sa.TEXT(),
               nullable=True)
    op.create_unique_constraint('users_clerk_user_id_key', 'users', ['clerk_user_id'])
    # `email` is no longer the auth identity key (`clerk_user_id`/`sub` is) — a Clerk
    # login and a pre-existing dev-mode user can legitimately share an email (e.g. a
    # dev->prod transition) without being the same account. Keeping this constraint
    # turned that collision into an uncaught IntegrityError (500) at login time
    # (issue #16 code review, `db.get_or_create_user_by_clerk_id`).
    op.drop_constraint('users_email_key', 'users', type_='unique')


def downgrade() -> None:
    """Downgrade schema."""
    op.create_unique_constraint('users_email_key', 'users', ['email'])
    op.drop_constraint('users_clerk_user_id_key', 'users', type_='unique')
    op.alter_column('users', 'email',
               existing_type=sa.TEXT(),
               nullable=False)
    op.drop_column('users', 'clerk_user_id')
