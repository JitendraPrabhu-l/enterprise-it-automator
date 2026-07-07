from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

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
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


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
