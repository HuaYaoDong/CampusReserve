from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from campus_reserve.auth.dependencies import CurrentPrincipal
from campus_reserve.bookings.infrastructure.sqlalchemy_repository import (
    SqlAlchemyBookingRepository,
)
from campus_reserve.bookings.models import Booking, BookingRequest
from campus_reserve.bookings.schemas import (
    BookingItemResponse,
    BookingRequestResponse,
    BookingResponse,
    CreateBookingRequest,
)
from campus_reserve.bookings.services.redis_reservation import (
    RedisAtomicReservationService,
    RedisReservationCode,
    RedisReservationCommand,
    RedisReservationKeyspace,
)
from campus_reserve.bookings.services.sync_booking import (
    ERROR_MESSAGES,
    BookingRuleError,
    SyncBookingService,
)
from campus_reserve.shared.config import get_settings
from campus_reserve.shared.database import get_session
from campus_reserve.shared.enums import BookingRequestStatus, UserRole
from campus_reserve.shared.redis import get_redis

router = APIRouter()
DatabaseSession = Annotated[AsyncSession, Depends(get_session)]


settings = get_settings()

REDIS_ERROR_MESSAGES = {
    **ERROR_MESSAGES,
    "BATCH_DATE_MISMATCH": "预约批次与日期不匹配",
    "BATCH_NOT_OPEN": "当前批次已暂停或关闭",
    "REDIS_STATE_INVALID": "预约库存状态异常，请稍后重试",
}


@router.post(
    "/booking-requests",
    response_model=BookingRequestResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_booking_request(
    payload: CreateBookingRequest,
    principal: CurrentPrincipal,
    response: Response,
) -> BookingRequestResponse:
    _require_student(principal.role)
    redis = get_redis()
    keyspace = RedisReservationKeyspace()
    service = RedisAtomicReservationService(
        redis,
        keyspace=keyspace,
        ttl_seconds=settings.booking_result_ttl_seconds,
    )
    result = await service.reserve(
        RedisReservationCommand(
            request_id=payload.request_id,
            user_id=principal.user_id,
            batch_id=payload.release_batch_id,
            service_date=payload.service_date,
            slot_ids=tuple(payload.slot_ids),
        )
    )
    if result.code is RedisReservationCode.IDEMPOTENCY_CONFLICT:
        response.status_code = status.HTTP_409_CONFLICT
        return BookingRequestResponse(
            request_id=payload.request_id,
            status=BookingRequestStatus.FAILED,
            error_code=result.code.value,
            error_message=REDIS_ERROR_MESSAGES[result.code.value],
            replayed=result.replayed,
            booking=None,
        )
    state = await redis.hgetall(keyspace.request(payload.request_id))
    if not result.reserved:
        response.status_code = _redis_error_status(result.code)
    result_response = _redis_request_response(
        payload.request_id,
        state,
        replayed=result.replayed,
        fallback_code=result.code.value,
    )
    if result_response.status is BookingRequestStatus.CONFIRMED:
        response.status_code = status.HTTP_200_OK
    return result_response


@router.get(
    "/booking-requests/{request_id}",
    response_model=BookingRequestResponse,
)
async def get_booking_request(
    request_id: UUID,
    principal: CurrentPrincipal,
    session: DatabaseSession,
) -> BookingRequestResponse:
    _require_student(principal.role)
    redis_state = await get_redis().hgetall(RedisReservationKeyspace().request(request_id))
    if redis_state:
        if redis_state.get("user_id") != str(principal.user_id):
            raise _not_found_request()
        redis_response = _redis_request_response(request_id, redis_state, replayed=True)
        if redis_response.status != BookingRequestStatus.CONFIRMED:
            return redis_response

    service = SyncBookingService(SqlAlchemyBookingRepository(session))
    try:
        request = await service.get_request(principal.user_id, request_id)
    except BookingRuleError as exc:
        if redis_state:
            return redis_response
        raise _booking_error(exc) from exc
    return _request_response(request, replayed=True)


@router.get("/bookings", response_model=list[BookingResponse])
async def list_my_bookings(
    principal: CurrentPrincipal,
    session: DatabaseSession,
) -> list[BookingResponse]:
    _require_student(principal.role)
    service = SyncBookingService(SqlAlchemyBookingRepository(session))
    bookings = await service.list_bookings(principal.user_id)
    return [_booking_response(booking) for booking in bookings]


@router.get("/bookings/{booking_id}", response_model=BookingResponse)
async def get_my_booking(
    booking_id: int,
    principal: CurrentPrincipal,
    session: DatabaseSession,
) -> BookingResponse:
    _require_student(principal.role)
    service = SyncBookingService(SqlAlchemyBookingRepository(session))
    try:
        booking = await service.get_booking(principal.user_id, booking_id)
    except BookingRuleError as exc:
        raise _booking_error(exc) from exc
    return _booking_response(booking)


def _request_response(
    request: BookingRequest,
    *,
    replayed: bool,
) -> BookingRequestResponse:
    return BookingRequestResponse(
        request_id=request.request_id,
        status=request.status,
        error_code=request.error_code,
        error_message=ERROR_MESSAGES.get(request.error_code or "") or None,
        replayed=replayed,
        booking=_booking_response(request.booking) if request.booking else None,
    )


def _booking_response(booking: Booking) -> BookingResponse:
    return BookingResponse(
        id=booking.id,
        request_id=booking.request_id,
        booking_no=booking.booking_no,
        status=booking.status,
        confirmed_at=booking.confirmed_at,
        created_at=booking.created_at,
        items=[
            BookingItemResponse(
                slot_id=item.slot_id,
                court_id=item.slot.court_id,
                court_code=item.slot.court.code,
                court_name=item.slot.court.name,
                service_date=item.service_date,
                start_at=item.start_at,
                end_at=item.end_at,
            )
            for item in sorted(booking.items, key=lambda item: item.start_at)
        ],
    )


def _require_student(role: UserRole) -> None:
    if role is not UserRole.STUDENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "STUDENT_ONLY", "message": "只有学生账号可以预约"},
        )


def _booking_error(error: BookingRuleError) -> HTTPException:
    not_found_codes = {"REQUEST_NOT_FOUND", "BOOKING_NOT_FOUND", "SLOTS_NOT_FOUND"}
    invalid_codes = {
        "INVALID_SLOT_COUNT",
        "SLOTS_MUST_BE_UNIQUE",
        "SLOTS_MUST_SHARE_DATE",
        "SLOTS_MUST_SHARE_BATCH",
    }
    if error.code in not_found_codes:
        status_code = status.HTTP_404_NOT_FOUND
    elif error.code in invalid_codes:
        status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    else:
        status_code = status.HTTP_409_CONFLICT
    return HTTPException(
        status_code=status_code,
        detail={
            "code": error.code,
            "message": str(error),
            "replayed": error.replayed,
        },
    )


def _redis_request_response(
    request_id: UUID,
    state: dict[str, str],
    *,
    replayed: bool,
    fallback_code: str | None = None,
) -> BookingRequestResponse:
    code = state.get("code") or fallback_code or "REDIS_STATE_INVALID"
    persistence_status = state.get("persistence_status")
    if persistence_status == BookingRequestStatus.CONFIRMED.value:
        request_status = BookingRequestStatus.CONFIRMED
        error_code = None
    elif persistence_status == BookingRequestStatus.FAILED.value:
        request_status = BookingRequestStatus.FAILED
        error_code = state.get("persistence_error_code") or code
    elif code == RedisReservationCode.RESERVED.value:
        request_status = BookingRequestStatus.PROCESSING
        error_code = None
    else:
        request_status = BookingRequestStatus.FAILED
        error_code = code
    return BookingRequestResponse(
        request_id=request_id,
        status=request_status,
        error_code=error_code,
        error_message=REDIS_ERROR_MESSAGES.get(error_code or "") or None,
        replayed=replayed,
        booking=None,
    )


def _redis_error_status(code: RedisReservationCode) -> int:
    if code in {
        RedisReservationCode.INVALID_SLOT_COUNT,
        RedisReservationCode.SLOTS_MUST_BE_UNIQUE,
        RedisReservationCode.BATCH_DATE_MISMATCH,
    }:
        return status.HTTP_422_UNPROCESSABLE_ENTITY
    if code is RedisReservationCode.SLOTS_NOT_FOUND:
        return status.HTTP_404_NOT_FOUND
    if code is RedisReservationCode.REDIS_STATE_INVALID:
        return status.HTTP_503_SERVICE_UNAVAILABLE
    return status.HTTP_409_CONFLICT


def _not_found_request() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "REQUEST_NOT_FOUND", "message": "预约请求不存在"},
    )
