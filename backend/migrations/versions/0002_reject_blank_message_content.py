"""Reject whitespace-only completed content, including tabs and newlines.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_messages_user_message"), "messages", type_="check")
    op.drop_constraint(op.f("ck_messages_completed_content"), "messages", type_="check")
    op.create_check_constraint(op.f("ck_messages_user_message"), "messages",
        "role != 'user' OR (status = 'completed' AND response_mode IS NULL AND content IS NOT NULL AND content ~ '[^[:space:]]')")
    op.create_check_constraint(op.f("ck_messages_completed_content"), "messages",
        "status != 'completed' OR (content IS NOT NULL AND content ~ '[^[:space:]]')")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_messages_user_message"), "messages", type_="check")
    op.drop_constraint(op.f("ck_messages_completed_content"), "messages", type_="check")
    op.create_check_constraint(op.f("ck_messages_user_message"), "messages",
        "role != 'user' OR (status = 'completed' AND response_mode IS NULL AND content IS NOT NULL AND btrim(content) <> '')")
    op.create_check_constraint(op.f("ck_messages_completed_content"), "messages",
        "status != 'completed' OR (content IS NOT NULL AND btrim(content) <> '')")
