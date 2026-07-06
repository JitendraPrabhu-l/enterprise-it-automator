import pytest
from fastapi import HTTPException

from app.api.auth import require_api_key, require_reviewer_token
from app.config import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_require_api_key_allows_when_unset(monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    await require_api_key(x_api_key=None)  # no exception


async def test_require_api_key_rejects_missing_header(monkeypatch):
    monkeypatch.setenv("API_KEY", "secret123")
    with pytest.raises(HTTPException) as exc:
        await require_api_key(x_api_key=None)
    assert exc.value.status_code == 401


async def test_require_api_key_rejects_wrong_key(monkeypatch):
    monkeypatch.setenv("API_KEY", "secret123")
    with pytest.raises(HTTPException) as exc:
        await require_api_key(x_api_key="wrong")
    assert exc.value.status_code == 401


async def test_require_api_key_accepts_correct_key(monkeypatch):
    monkeypatch.setenv("API_KEY", "secret123")
    await require_api_key(x_api_key="secret123")  # no exception


@pytest.fixture
async def isolated_db(monkeypatch, tmp_path):
    """require_reviewer_token goes through app.db.session's module-level
    engine/session-factory singletons, same pattern as
    test_fanout.py/test_sla_sweep.py's isolated_db fixture.
    """
    from app.db import session as db_session_module

    db_path = tmp_path / "auth_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None
    await db_session_module.init_db()
    yield db_session_module
    db_session_module._engine = None
    db_session_module._session_factory = None
    get_settings.cache_clear()


async def test_require_reviewer_token_rejects_missing_header(isolated_db):
    with pytest.raises(HTTPException) as exc:
        await require_reviewer_token(x_reviewer_token=None)
    assert exc.value.status_code == 401


async def test_require_reviewer_token_rejects_unknown_token(isolated_db):
    with pytest.raises(HTTPException) as exc:
        await require_reviewer_token(x_reviewer_token="not-a-real-token")
    assert exc.value.status_code == 401


async def test_require_reviewer_token_accepts_valid_token_and_returns_reviewer(isolated_db):
    from app.db.models import Reviewer, ReviewerRole

    async with isolated_db.session_scope() as session:
        session.add(Reviewer(username="mchen", role=ReviewerRole.MANAGER, token="real-token-123"))

    reviewer = await require_reviewer_token(x_reviewer_token="real-token-123")
    assert reviewer.username == "mchen"
    assert reviewer.role == ReviewerRole.MANAGER


async def test_require_reviewer_token_does_not_authenticate_by_username(isolated_db):
    """A request supplying the reviewer's USERNAME instead of their TOKEN
    must be rejected — this is the exact impersonation gap the token
    mechanism closes. Guards against a regression back to trusting a
    client-supplied name."""
    from app.db.models import Reviewer, ReviewerRole

    async with isolated_db.session_scope() as session:
        session.add(Reviewer(username="admin", role=ReviewerRole.IT_ADMIN, token="admins-real-token"))

    with pytest.raises(HTTPException) as exc:
        await require_reviewer_token(x_reviewer_token="admin")
    assert exc.value.status_code == 401
