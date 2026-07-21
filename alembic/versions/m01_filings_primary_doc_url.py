"""filings.primary_doc_url: persist EDGAR primary-doc URL at discover-time.

Revision ID: m01
Revises: m00
Create Date: 2026-07-21

Adds a nullable ``primary_doc_url text`` column to ``filings`` so ``discover()``
can capture the primary-document URL once (from ``FilingMetadata`` returned by
``list_filings()``) and ``_process_one`` reads it back instead of re-listing
EDGAR. The re-list used a single-day window that fell into SEC's ~1-day
``filingTo`` dead zone (Session-36 defect).

Nullable is deliberate: the already-``processed`` rows never re-list and stay
NULL. Only pending/future rows require it. The column is added with
``IF NOT EXISTS`` so this migration is idempotent / re-runnable.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "m01"
down_revision: str | None = "m00"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE filings ADD COLUMN IF NOT EXISTS primary_doc_url text;")


def downgrade() -> None:
    op.execute("ALTER TABLE filings DROP COLUMN IF EXISTS primary_doc_url;")
