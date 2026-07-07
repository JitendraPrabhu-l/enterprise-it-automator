from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

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
