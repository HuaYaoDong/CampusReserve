import asyncio
from collections.abc import Awaitable, Callable

from campus_reserve.shared.database import check_database
from campus_reserve.shared.redis import check_redis
from campus_reserve.system.schemas.health import ComponentHealth, HealthResponse


async def _check(component: Callable[[], Awaitable[None]]) -> ComponentHealth:
    try:
        await component()
        return ComponentHealth(status="ok")
    except Exception as exc:
        return ComponentHealth(status="error", detail=type(exc).__name__)


class HealthService:
    async def check(self) -> HealthResponse:
        database, redis = await asyncio.gather(_check(check_database), _check(check_redis))
        overall = "ok" if database.status == redis.status == "ok" else "degraded"
        return HealthResponse(status=overall, database=database, redis=redis)

