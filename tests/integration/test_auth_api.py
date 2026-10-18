"""登录和当前用户接口的集成测试。"""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from apps.api.main import app
from campus_reserve.shared.config import get_settings
from campus_reserve.shared.database import get_session
from campus_reserve.users.models import User


@pytest.mark.integration
async def test_mock_login_and_current_user_flow() -> None:
    test_engine = create_async_engine(get_settings().postgres_dsn, poolclass=NullPool)
    test_sessions = async_sessionmaker(test_engine, expire_on_commit=False)

    async def override_session():
        async with test_sessions() as session, session.begin():
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            missing_token = await client.get("/api/v1/auth/me")
            assert missing_token.status_code == 401
            assert missing_token.headers["www-authenticate"] == "Bearer"

            invalid_login = await client.post(
                "/api/v1/auth/login",
                json={"username": "student001", "password": "WrongPass123"},
            )
            assert invalid_login.status_code == 401
            assert invalid_login.json()["detail"]["code"] == "INVALID_CREDENTIALS"

            login = await client.post(
                "/api/v1/auth/login",
                json={"username": "Student001", "password": "Student@123"},
            )
            assert login.status_code == 200
            payload = login.json()
            assert payload["token_type"] == "bearer"
            assert payload["expires_in"] == 1800
            assert payload["user"]["username"] == "student001"
            assert payload["user"]["role"] == "STUDENT"

            profile = await client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {payload['access_token']}"},
            )
            assert profile.status_code == 200
            assert profile.json()["student_no"] == "20260001"

        async with test_sessions() as session:
            user = await session.scalar(select(User).where(User.username == "student001"))
            assert user is not None
            assert user.last_login_at is not None
    finally:
        app.dependency_overrides.pop(get_session, None)
        await test_engine.dispose()
