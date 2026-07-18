from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db.models import Base

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _ensure_sqlite_dir_exists(database_url: str) -> None:
    """SQLite (via aiosqlite) does not auto-create the parent directory of its
    file — only the file itself — so a fresh checkout without data/ present
    would fail on first run with 'unable to open database file'.

    Strips exactly ONE leading slash (the sqlite:/// URL syntax's own
    separator), not all of them — a relative-path URL
    ("sqlite+aiosqlite:///./data/x.db") parses to path "/./data/x.db" (one
    leading slash to strip), but an absolute-path URL
    ("sqlite+aiosqlite:////tmp/x.db", four slashes) parses to path
    "//tmp/x.db" (two leading slashes: the URL separator, then the path's
    own leading "/"). `.lstrip("/")` would strip BOTH in the absolute case,
    silently turning "/tmp/x.db" into the relative path "tmp/x.db" — caught
    live via CI running on Linux (pytest's tmp_path fixture produces
    absolute POSIX paths); Windows paths never hit this because they don't
    start with "/" the same way.
    """
    if not database_url.startswith("sqlite"):
        return
    db_path = urlparse(database_url).path.removeprefix("/")
    if db_path and db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)


def get_engine():
    global _engine
    if _engine is None:
        database_url = get_settings().database_url
        _ensure_sqlite_dir_exists(database_url)
        _engine = create_async_engine(database_url, echo=False)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def init_db() -> None:
    """Creates every table (and, on Postgres, the ENUM types they depend on)
    if they don't already exist.

    Postgres ENUM types are the one piece of this that isn't safely
    idempotent under concurrent callers: SQLAlchemy's create_all checks
    "does this type exist?" and then emits CREATE TYPE, but that check-then-
    create isn't atomic. This app's Dockerfile runs 2 gunicorn workers, each
    calling init_db() independently at startup — verified live against a
    real Postgres (Neon) database that starting from a genuinely empty
    schema, both workers' create_all calls race on the same CREATE TYPE
    statement, and the loser crashes with IntegrityError (unique violation
    on pg_type), taking the whole gunicorn master down with it
    ("Worker failed to boot"). SQLite has no equivalent race (no ENUM
    concept at the DB level), so this was invisible under local/SQLite-only
    testing and only surfaced once this ran against Postgres for real.

    The fix: treat "some other worker already created it" as success, not
    failure. IntegrityError is a stable, driver-agnostic SQLAlchemy class
    (unlike the underlying asyncpg/psycopg-specific exception types it
    wraps), so this stays correct regardless of which Postgres driver is in
    use without importing driver internals here.
    """
    engine = get_engine()
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except IntegrityError:
        pass
    await _ensure_column(engine, table="tickets", column="submitted_by_client_id", ddl_type="INTEGER")
    await _ensure_column(engine, table="api_clients", column="data_last_purged_at", ddl_type="TIMESTAMP")
    await _ensure_column(engine, table="employee_users", column="owned_by_client_id", ddl_type="INTEGER")
    await _ensure_column(engine, table="reviewers", column="telegram_chat_id", ddl_type="VARCHAR(64)")
    await _ensure_column(engine, table="reviewers", column="email", ddl_type="VARCHAR(128)")
    await _ensure_column(engine, table="approvals", column="reviewer_auth_method", ddl_type="VARCHAR(16)")
    await _ensure_column(engine, table="approvals", column="reviewer_oidc_subject", ddl_type="VARCHAR(128)")
    await _ensure_column(engine, table="audit_log", column="prev_hash", ddl_type="VARCHAR(64)")
    await _ensure_column(engine, table="audit_log", column="entry_hash", ddl_type="VARCHAR(64)")
    await _ensure_column(engine, table="api_clients", column="tokens_used_today", ddl_type="INTEGER")
    await _ensure_column(engine, table="api_clients", column="token_count_reset_at", ddl_type="TIMESTAMP")


async def _ensure_column(engine, *, table: str, column: str, ddl_type: str) -> None:
    """create_all (above) only creates MISSING TABLES — it never alters an
    EXISTING table to add a newly-modeled column, so a column added to a
    model after its table already exists on a live database (has happened
    twice now: Ticket.submitted_by_client_id, ApiClient.data_last_purged_at)
    needs an explicit, self-healing ADD COLUMN step, or every read/write
    referencing it crashes with "column does not exist" the moment this
    deploys against an already-provisioned database.

    Dialect-agnostic (checks via SQLAlchemy's inspector rather than a raw
    "ALTER TABLE ... ADD COLUMN IF NOT EXISTS", whose syntax/support
    differs between SQLite and Postgres) and safe to call on every
    startup, on both a fresh database (table doesn't exist yet — create_all
    above just made it correctly, with every current column, so this is a
    no-op) and an already-migrated one (column already present — no-op).

    `ddl_type` is deliberately a plain, portable SQL type name (INTEGER,
    TIMESTAMP) rather than each dialect's Postgres/SQLite-specific spelling
    — both accept these as-is for a bare ADD COLUMN with no constraints.
    """
    def _has_column(sync_conn) -> bool:
        inspector = inspect(sync_conn)
        if table not in inspector.get_table_names():
            return True  # table doesn't exist yet — create_all above just made it correctly
        columns = {c["name"] for c in inspector.get_columns(table)}
        return column in columns

    async with engine.begin() as conn:
        already_present = await conn.run_sync(_has_column)
        if already_present:
            return
        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def try_advisory_lock(lock_id: int) -> AsyncIterator[bool]:
    """Best-effort cross-replica mutual exclusion via Postgres session-level
    advisory locks: yields True if this process holds lock_id for the
    duration of the block, False if another replica already does (caller
    should skip its pass, not wait — these guard periodic background jobs
    where "someone is doing it" is all that matters).

    On non-Postgres engines yields True unconditionally: SQLite here means
    a single-process deployment (a shared SQLite file across replicas is
    not a supported topology anywhere in this app), so there's nobody to
    exclude and the sweep behaves exactly as it did before this existed.

    Correctness notes, learned from the advisory-lock footgun list:
    - pg_try_advisory_lock is SESSION-scoped, so the lock lives exactly as
      long as this connection — the `async with engine.connect()` block —
      and the explicit finally-unlock matters because pooled connections
      are REUSED, not closed, on release; without it the lock would leak
      into the pool and never be released until pool recycle.
    - If the process dies mid-pass, Postgres releases the session's locks
      when the connection drops — no stale-lock janitor needed.
    - The lock connection is deliberately separate from the sweep's own
      session_scope() sessions; holding it open serializes nothing else.
    """
    engine = get_engine()
    if engine.dialect.name != "postgresql":
        yield True
        return
    async with engine.connect() as conn:
        acquired = (
            await conn.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": lock_id})
        ).scalar()
        try:
            yield bool(acquired)
        finally:
            if acquired:
                await conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": lock_id})
