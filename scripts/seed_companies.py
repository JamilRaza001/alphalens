"""Seed 10 S&P 500 companies into the companies table. Idempotent (UPSERT)."""

from __future__ import annotations

import os
import sys

import psycopg
from dotenv import load_dotenv

COMPANIES: list[tuple[str, str, str, str]] = [
    ("AAPL", "Apple Inc.", "0000320193", "Technology"),
    ("MSFT", "Microsoft Corporation", "0000789019", "Technology"),
    ("GOOGL", "Alphabet Inc.", "0001652044", "Communication Services"),
    ("AMZN", "Amazon.com Inc.", "0001018724", "Consumer Discretionary"),
    ("NVDA", "NVIDIA Corporation", "0001045810", "Technology"),
    ("META", "Meta Platforms Inc.", "0001326801", "Communication Services"),
    ("TSLA", "Tesla Inc.", "0001318605", "Consumer Discretionary"),
    ("BRK-B", "Berkshire Hathaway Inc.", "0001067983", "Financials"),
    ("JPM", "JPMorgan Chase & Co.", "0000019617", "Financials"),
    ("V", "Visa Inc.", "0001403161", "Financials"),
]

UPSERT_SQL = """
    INSERT INTO companies (ticker, name, cik, sector)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (ticker) DO UPDATE
        SET name   = EXCLUDED.name,
            cik    = EXCLUDED.cik,
            sector = EXCLUDED.sector
"""


def seed_companies(database_url: str) -> int:
    """UPSERT 10 S&P top companies. Returns rows affected."""
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.executemany(UPSERT_SQL, COMPANIES)
            count = cur.rowcount
        conn.commit()
    return count


def main() -> None:
    load_dotenv()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL not set in environment or .env", file=sys.stderr)
        sys.exit(1)

    try:
        count = seed_companies(database_url)
        print(f"Seeded {count} row(s) into companies.")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
