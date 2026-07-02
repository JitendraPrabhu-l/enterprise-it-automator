from app.db.session import _ensure_sqlite_dir_exists


def test_ensure_sqlite_dir_exists_creates_missing_parent(tmp_path):
    db_path = tmp_path / "nested" / "deeper" / "app.db"
    assert not db_path.parent.exists()

    _ensure_sqlite_dir_exists(f"sqlite+aiosqlite:///{db_path.as_posix()}")

    assert db_path.parent.exists()


def test_ensure_sqlite_dir_exists_noop_when_parent_already_exists(tmp_path):
    db_path = tmp_path / "app.db"

    _ensure_sqlite_dir_exists(f"sqlite+aiosqlite:///{db_path.as_posix()}")  # must not raise

    assert tmp_path.exists()


def test_ensure_sqlite_dir_exists_ignores_in_memory_db():
    _ensure_sqlite_dir_exists("sqlite+aiosqlite:///:memory:")  # must not raise, nothing to create


def test_ensure_sqlite_dir_exists_ignores_non_sqlite_url():
    _ensure_sqlite_dir_exists("postgresql+asyncpg://user:pw@localhost:5432/db")  # must not raise
