from collections.abc import AsyncIterator
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from campus_reserve.admin.schemas import (
    AdminBookingView,
    AdminOverview,
    AuditView,
    BatchView,
    CheckInRequest,
    CheckInView,
    CourtPatch,
    CourtView,
    ExceptionUpsert,
    ExceptionView,
    GenerationPreview,
    GenerationRequest,
    TemplateCreate,
    TemplatePatch,
    TemplateView,
    VenuePatch,
    VenueView,
)
from campus_reserve.admin.services import AdminService
from campus_reserve.auth.dependencies import CurrentPrincipal
from campus_reserve.shared.database import async_session_factory
from campus_reserve.shared.enums import UserRole, UserStatus
from campus_reserve.shared.redis import get_redis
from campus_reserve.users.models import User

router = APIRouter()


async def get_admin_session() -> AsyncIterator[AsyncSession]:
    async with async_session_factory() as session:
        yield session


AdminSession = Annotated[AsyncSession, Depends(get_admin_session)]
AdminRedis = Annotated[Redis, Depends(get_redis)]


async def get_admin_service(
    principal: CurrentPrincipal, request: Request, session: AdminSession, redis: AdminRedis,
) -> AsyncIterator[AdminService]:
    user = await session.get(User, principal.user_id)
    if user is None or user.status is not UserStatus.ACTIVE or user.role != principal.role:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHORIZED", "message": "登录状态无效"},
        )
    if user.role not in (UserRole.VENUE_ADMIN, UserRole.PLATFORM_ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "ADMIN_REQUIRED", "message": "需要管理员权限"},
        )
    yield AdminService(session, redis, principal, request.client.host if request.client else None)


Admin = Annotated[AdminService, Depends(get_admin_service)]


def platform_only(service: AdminService) -> None:
    if service.principal.role is not UserRole.PLATFORM_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "PLATFORM_ADMIN_REQUIRED", "message": "需要平台管理员权限"},
        )


@router.get("/overview", response_model=AdminOverview)
async def overview(service: Admin) -> AdminOverview:
    return await service.overview()


@router.patch("/venue", response_model=VenueView)
async def update_venue(payload: VenuePatch, service: Admin) -> VenueView:
    return await service.update_venue(payload)


@router.patch("/courts/{court_id}", response_model=CourtView)
async def update_court(court_id: int, payload: CourtPatch, service: Admin) -> CourtView:
    return await service.update_court(court_id, payload)


@router.post("/opening-templates", response_model=TemplateView)
async def create_template(payload: TemplateCreate, service: Admin) -> TemplateView:
    platform_only(service)
    return await service.create_template(payload)


@router.patch("/opening-templates/{template_id}", response_model=TemplateView)
async def update_template(
    template_id: int, payload: TemplatePatch, service: Admin,
) -> TemplateView:
    platform_only(service)
    return await service.update_template(template_id, payload)


@router.put("/calendar-exceptions", response_model=ExceptionView)
async def upsert_exception(payload: ExceptionUpsert, service: Admin) -> ExceptionView:
    return await service.upsert_exception(payload)


@router.post("/slot-generation-jobs", response_model=GenerationPreview)
async def generate_slots(payload: GenerationRequest, service: Admin) -> GenerationPreview:
    return await service.preview_or_generate(payload.service_date, payload.preview_only)


@router.post("/release-batches/{batch_id}/publish", response_model=BatchView)
async def publish_batch(batch_id: int, service: Admin) -> BatchView:
    return await service.publish_batch(batch_id)


@router.post("/release-batches/{batch_id}/{action}", response_model=BatchView)
async def change_batch(batch_id: int, action: str, service: Admin) -> BatchView:
    return await service.change_batch(batch_id, action)


@router.get("/bookings", response_model=list[AdminBookingView])
async def search_bookings(
    service: Admin,
    booking_no: str | None = None,
    username: str | None = None,
    service_date: date | None = None,
    court_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[AdminBookingView]:
    return await service.search_bookings(
        booking_no=booking_no, username=username, service_date=service_date,
        court_id=court_id, limit=limit,
    )


@router.post("/bookings/{booking_id}/check-in", response_model=CheckInView)
async def check_in(booking_id: int, payload: CheckInRequest, service: Admin) -> CheckInView:
    return await service.check_in(booking_id, payload)


@router.get("/audit-logs", response_model=list[AuditView])
async def audit_logs(
    service: Admin, limit: int = Query(default=100, ge=1, le=500),
) -> list[AuditView]:
    return await service.audit_logs(limit)
