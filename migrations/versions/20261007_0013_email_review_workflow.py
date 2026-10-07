"""Review workflow for generated emails.

New emails start in `in_review`. A person approves (`approved`) or rejects (`rejected`) each one in the review
dashboard; only approved emails are ever sent. Editing an approved email sends it back to review.
Statuses: in_review -> approved -> sending -> sent -> opened, plus rejected and failed. `draft` is replaced by
`in_review`.

Revision ID: 20261007_0013
Revises: 20261007_0012
Create Date: 2026-10-07
"""

from alembic import op
import sqlalchemy as sa

revision = "20261007_0013"
down_revision = "20261007_0012"
branch_labels = None
depends_on = None

STATUSES = ("in_review", "approved", "rejected", "sending", "sent", "opened", "failed")
OLD_STATUSES = ("draft", "sending", "sent", "opened", "failed")


def _check(statuses):
    return "status IN (" + ", ".join(f"'{s}'" for s in statuses) + ")"


def upgrade():
    op.drop_constraint("ck_emails_status", "emails", type_="check")
    op.execute("UPDATE emails SET status = 'in_review' WHERE status = 'draft'")
    op.create_check_constraint("ck_emails_status", "emails", _check(STATUSES))
    op.alter_column("emails", "status", server_default="in_review")
    op.add_column("emails", sa.Column("reviewed_at", sa.DateTime(timezone=True)))
    op.add_column("emails", sa.Column("review_note", sa.Text()))
    op.add_column("emails", sa.Column("edited_at", sa.DateTime(timezone=True)))


def downgrade():
    op.drop_column("emails", "edited_at")
    op.drop_column("emails", "review_note")
    op.drop_column("emails", "reviewed_at")
    op.drop_constraint("ck_emails_status", "emails", type_="check")
    op.execute("UPDATE emails SET status = 'draft' WHERE status IN ('in_review', 'approved', 'rejected')")
    op.create_check_constraint("ck_emails_status", "emails", _check(OLD_STATUSES))
    op.alter_column("emails", "status", server_default="draft")
