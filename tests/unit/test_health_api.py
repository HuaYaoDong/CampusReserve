"""健康接口单元测试。"""

from httpx import ASGITransport, AsyncClient

from apps.api.main import app


async def test_live_endpoint() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/system/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
