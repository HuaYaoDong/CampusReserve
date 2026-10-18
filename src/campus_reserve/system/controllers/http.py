"""健康接口：汇总数据库和 Redis 可用性。"""

from fastapi import APIRouter, Response, status

from campus_reserve.system.schemas.health import HealthResponse, LiveResponse
from campus_reserve.system.services.health import HealthService

router = APIRouter()


@router.get("/health/live", response_model=LiveResponse)
async def live() -> LiveResponse:
    return LiveResponse(status="ok")


@router.get("/health", response_model=HealthResponse)
async def ready(response: Response) -> HealthResponse:
    result = await HealthService().check()
    if result.status != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return result

