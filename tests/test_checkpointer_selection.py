"""app/agent/runner.py picks AsyncPostgresSaver vs AsyncSqliteSaver based on
whether CHECKPOINT_DB_PATH looks like a Postgres connection string — see
docker-compose.yml, which now points CHECKPOINT_DB_PATH at Postgres via a
plain "postgresql://" URL (psycopg-style, not "+asyncpg").
"""

from app.agent.runner import _is_postgres_url


def test_sqlite_file_path_is_not_postgres():
    assert _is_postgres_url("./data/it_automator_checkpoints.db") is False


def test_postgresql_scheme_is_postgres():
    assert _is_postgres_url("postgresql://itauto:pw@postgres:5432/it_automator") is True


def test_postgres_scheme_is_postgres():
    assert _is_postgres_url("postgres://itauto:pw@postgres:5432/it_automator") is True


def test_asyncpg_scheme_is_not_matched():
    """"postgresql+asyncpg://" is the APP DB's driver string (app/db/session.py),
    a different backend than the checkpointer's psycopg-based one — a config
    that accidentally reuses DATABASE_URL's value here should fall through to
    the sqlite branch and fail loudly (file path parsing), not silently
    connect with the wrong driver semantics.
    """
    assert _is_postgres_url("postgresql+asyncpg://itauto:pw@postgres:5432/it_automator") is False
