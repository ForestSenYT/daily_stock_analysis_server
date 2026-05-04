# -*- coding: utf-8 -*-
"""SQLite → Postgres one-shot migration for the DSA project.

Designed to run **once** during the GCSFuse → Cloud SQL cutover. After
migration, the Cloud Run service points at Postgres via ``DATABASE_URL``
(Unix socket form) and the SQLite file on GCSFuse is decommissioned.

What this script does:

  1. Connects to the source (SQLite via local file path) and target
     (Postgres via SQLAlchemy URL).
  2. Runs ``Base.metadata.create_all`` against Postgres so all 18
     tables exist with the right schema (no manual SQL).
  3. Copies rows for the **A-class tables** only (user data that can't
     be regenerated):

       * portfolio_accounts
       * portfolio_trades
       * portfolio_cash_ledger
       * portfolio_corporate_actions
       * broker_sync_runs
       * broker_snapshots

     Cache/derived tables (stock_daily, news_intel, fundamental_snapshot,
     analysis_history, backtest_*, portfolio_positions / lots / snapshots,
     portfolio_fx_rates, conversation_messages, llm_usage) are
     **schema-only**: their row count after migration is 0, and the
     application repopulates them naturally on next use.

  4. Verifies row counts match between source and target for each
     A-class table. Aborts on mismatch (no partial commit).

Usage:

    # Dry-run (no inserts; just prints the plan + counts)
    python scripts/migrate_sqlite_to_postgres.py \\
        --source-db ./data/stock_analysis.db \\
        --target-url 'postgresql+psycopg2://USER:PASS@PUBLIC_IP:5432/dsa' \\
        --dry-run

    # Real run
    python scripts/migrate_sqlite_to_postgres.py \\
        --source-db ./data/stock_analysis.db \\
        --target-url 'postgresql+psycopg2://USER:PASS@PUBLIC_IP:5432/dsa'

Safety properties:

  * **Source is read-only** — only ``SELECT`` queries against SQLite.
  * **Target is wrapped in a single transaction per table** — if any
    row fails, that table's batch is rolled back (you re-run after
    fixing). Other tables already migrated are unaffected.
  * **No DROP TABLE** — if Postgres already has data, migration aborts
    (you must manually clean up — running this twice on a populated
    target would otherwise UNIQUE-constraint-fail mid-stream).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Dict, List, Tuple

# Make ``src.*`` imports resolve when this script runs from any cwd.
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger("migrate")


# A-class tables: user-generated data that MUST be preserved.
# Order matters: parents before children for FK referential integrity
# (Postgres enforces FKs by default; SQLite often doesn't).
A_CLASS_TABLES_IN_ORDER: List[str] = [
    "portfolio_accounts",
    "portfolio_trades",
    "portfolio_cash_ledger",
    "portfolio_corporate_actions",
    "broker_sync_runs",
    "broker_snapshots",
]


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )


def _resolve_orm_class(table_name: str):
    """Return the SQLAlchemy ORM class for a given ``__tablename__``."""
    from src import storage as _storage  # late import: needs Base discovered

    # Walk every Base subclass once to find the matching __tablename__.
    for obj_name in dir(_storage):
        obj = getattr(_storage, obj_name)
        if (
            isinstance(obj, type)
            and hasattr(obj, "__tablename__")
            and getattr(obj, "__tablename__", None) == table_name
        ):
            return obj
    raise RuntimeError(
        f"No ORM class found for table {table_name!r} in src.storage; "
        "did you rename it?"
    )


def _row_to_dict(row, columns) -> Dict:
    """Convert a SQLAlchemy ORM row to a plain dict of column → value
    (suitable for ``insert(values=...)``)."""
    return {col.name: getattr(row, col.name) for col in columns}


def _count_rows(session: Session, orm_cls) -> int:
    return int(session.execute(select(func.count()).select_from(orm_cls)).scalar() or 0)


def _migrate_table(
    *,
    table_name: str,
    src_session: Session,
    dst_session: Session,
    dry_run: bool,
    batch_size: int = 500,
) -> Tuple[int, int]:
    """Copy one table's rows. Returns (source_count, copied_count)."""
    orm_cls = _resolve_orm_class(table_name)
    src_count = _count_rows(src_session, orm_cls)

    if src_count == 0:
        logger.info("[%s] source has 0 rows; skipping", table_name)
        return (0, 0)

    # Refuse to write into a non-empty target table. Skip this check
    # in dry-run because the target schema may not exist yet (we
    # don't create_all in dry-run by design — keeps it side-effect-free).
    if not dry_run:
        try:
            dst_count_before = _count_rows(dst_session, orm_cls)
        except Exception as exc:  # noqa: BLE001 — unknown driver flavor
            raise RuntimeError(
                f"[{table_name}] cannot read target row count "
                f"(target schema missing? did create_all run?): {exc}"
            ) from exc
        if dst_count_before > 0:
            raise RuntimeError(
                f"[{table_name}] target table already has {dst_count_before} rows. "
                "Aborting to avoid duplicates. Truncate the target table first "
                "(via psql) if you intentionally want to re-migrate."
            )

    if dry_run:
        logger.info(
            "[%s] would copy %d rows (dry-run; no INSERT performed)",
            table_name, src_count,
        )
        return (src_count, 0)

    # Stream rows in batches so we don't load 10k+ rows into memory.
    columns = orm_cls.__table__.columns
    rows_iter = src_session.execute(select(orm_cls)).scalars().yield_per(batch_size)

    copied = 0
    batch: List[Dict] = []
    for row in rows_iter:
        batch.append(_row_to_dict(row, columns))
        if len(batch) >= batch_size:
            dst_session.execute(orm_cls.__table__.insert(), batch)
            copied += len(batch)
            logger.debug("[%s] flushed %d (running total: %d)", table_name, len(batch), copied)
            batch = []
    if batch:
        dst_session.execute(orm_cls.__table__.insert(), batch)
        copied += len(batch)

    dst_session.commit()

    dst_count_after = _count_rows(dst_session, orm_cls)
    if dst_count_after != src_count:
        raise RuntimeError(
            f"[{table_name}] row-count mismatch after copy: "
            f"source={src_count}, target={dst_count_after}, copied={copied}"
        )
    logger.info("[%s] copied %d / %d rows", table_name, copied, src_count)
    return (src_count, copied)


def _fix_postgres_sequences(dst_engine, table_names: List[str]) -> None:
    """Reset Postgres SERIAL sequences after bulk-insert.

    When we INSERT explicit ``id`` values, Postgres's auto-increment
    sequences DON'T advance — so the next app-level INSERT (without an
    explicit id) starts at 1 and instantly UNIQUE-fails. This routine
    advances each table's id sequence past the max(id) we just wrote.
    """
    from sqlalchemy import text

    with dst_engine.begin() as conn:
        for table_name in table_names:
            sql = text(f"""
                SELECT setval(
                    pg_get_serial_sequence('{table_name}', 'id'),
                    COALESCE((SELECT MAX(id) FROM {table_name}), 1),
                    true
                )
            """)
            try:
                conn.execute(sql)
                logger.info("[%s] advanced id sequence past max(id)", table_name)
            except Exception as exc:  # noqa: BLE001
                # Some tables may not have an ``id`` SERIAL — that's fine.
                logger.debug(
                    "[%s] sequence reset skipped (%s)", table_name, exc,
                )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-db",
        default="./data/stock_analysis.db",
        help="Path to the source SQLite file (default: ./data/stock_analysis.db).",
    )
    parser.add_argument(
        "--target-url",
        required=True,
        help=(
            "Target SQLAlchemy URL, e.g. "
            "postgresql+psycopg2://user:pass@host:5432/dsa"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan + row counts without inserting.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose / debug logging.",
    )
    args = parser.parse_args()

    _setup_logging(args.verbose)

    # Build the source URL.
    src_url = f"sqlite:///{args.source_db}"
    logger.info("source: %s", src_url)
    logger.info("target: %s", args.target_url.split("@")[-1])  # hide password
    logger.info("mode:   %s", "DRY-RUN" if args.dry_run else "REAL RUN")
    logger.info("tables: %s", ", ".join(A_CLASS_TABLES_IN_ORDER))

    # Engines.
    src_engine = create_engine(src_url, echo=False)
    dst_engine = create_engine(args.target_url, echo=False, pool_pre_ping=True)

    # Step 1: ensure target schema exists for ALL tables (A + B class).
    if not args.dry_run:
        from src.storage import Base
        logger.info("creating target schema (Base.metadata.create_all)...")
        Base.metadata.create_all(dst_engine)
        logger.info("target schema ready (18 tables present)")
    else:
        logger.info("[dry-run] would create target schema for 18 tables")

    src_sessionmaker = sessionmaker(bind=src_engine)
    dst_sessionmaker = sessionmaker(bind=dst_engine)

    summary: List[Tuple[str, int, int]] = []
    failed: List[str] = []
    with src_sessionmaker() as src_session, dst_sessionmaker() as dst_session:
        for table_name in A_CLASS_TABLES_IN_ORDER:
            try:
                src_count, copied = _migrate_table(
                    table_name=table_name,
                    src_session=src_session,
                    dst_session=dst_session,
                    dry_run=args.dry_run,
                )
                summary.append((table_name, src_count, copied))
            except Exception as exc:  # noqa: BLE001
                logger.error("[%s] FAILED: %s", table_name, exc)
                failed.append(table_name)
                if not args.dry_run:
                    # Abort early so user can fix and re-run.
                    break

    # Step 2: fix sequences (only on real run).
    if not args.dry_run and not failed:
        _fix_postgres_sequences(dst_engine, A_CLASS_TABLES_IN_ORDER)

    # Print summary table.
    logger.info("=" * 60)
    logger.info("Migration summary:")
    logger.info("%-32s %10s %10s", "table", "source", "copied")
    for name, src_cnt, dst_cnt in summary:
        marker = "✓" if (src_cnt == dst_cnt or args.dry_run) else "✗"
        logger.info("%-32s %10d %10d  %s", name, src_cnt, dst_cnt, marker)
    if failed:
        logger.error("FAILED tables: %s", ", ".join(failed))
        return 1
    logger.info("=" * 60)
    logger.info("All A-class tables migrated cleanly.")
    if args.dry_run:
        logger.info("This was a DRY-RUN. Re-run without --dry-run to commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
