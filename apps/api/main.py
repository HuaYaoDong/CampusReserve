"""HTTP 服务入口：装配各业务路由、错误处理、健康指标和启动关闭钩子。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import campus_reserve.models  # noqa: F401
from campus_reserve.admin.controllers.http import router as admin_router
from campus_reserve.admin.services import AdminRuleError
from campus_reserve.auth.controllers.http import router as auth_router
from campus_reserve.bookings.controllers.http import router as booking_router
from campus_reserve.inventory.controllers.http import router as venue_router
from campus_reserve.shared.config import get_settings
from campus_reserve.shared.database import close_database
from campus_reserve.shared.logging import configure_logging, get_logger
from campus_reserve.shared.metrics import PrometheusMiddleware, metrics_endpoint
from campus_reserve.shared.redis import close_redis
from campus_reserve.system.controllers.http import router as system_router

settings = get_settings()
configure_logging(settings.log_level)
logger = get_logger(__name__)


# 进程退出时关闭共享 Redis 和数据库连接，避免测试及重启时泄漏连接。
@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    logger.info("api_started", extra={"app_env": settings.app_env})
    yield
    await close_redis()
    await close_database()
    logger.info("api_stopped")


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:8080"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(PrometheusMiddleware)
app.include_router(admin_router, prefix="/api/v1/admin", tags=["administration"])
app.include_router(auth_router, prefix="/api/v1/auth", tags=["authentication"])
app.include_router(booking_router, prefix="/api/v1", tags=["bookings"])
app.include_router(venue_router, prefix="/api/v1/venues", tags=["venues and inventory"])
app.include_router(system_router, prefix="/api/v1/system", tags=["system"])
app.add_api_route("/metrics", metrics_endpoint, include_in_schema=False)


@app.exception_handler(AdminRuleError)
async def admin_rule_error_handler(_: object, exc: AdminRuleError) -> JSONResponse:
    return JSONResponse(
        status_code=404 if exc.not_found else 409,
        content={"detail": {"code": exc.code, "message": str(exc)}},
    )
