from datetime import date, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from campus_reserve.auth.dependencies import CurrentPrincipal
from campus_reserve.inventory.infrastructure.sqlalchemy_repository import (
    SqlAlchemyInventoryRepository,
)
from campus_reserve.inventory.schemas import (
    CourtInventoryResponse,
    CourtSummary,
    InventoryResponse,
    InventorySlotResponse,
    VenueSummary,
)
from campus_reserve.inventory.services.inventory import InventoryNotFoundError, InventoryService
from campus_reserve.shared.database import get_session
from campus_reserve.shared.enums import BookingSlotStatus, ReleaseBatchStatus

router = APIRouter()
DatabaseSession = Annotated[AsyncSession, Depends(get_session)]


@router.get("", response_model=list[VenueSummary])
async def list_venues(
    _principal: CurrentPrincipal,
    session: DatabaseSession,
) -> list[VenueSummary]:
    service = InventoryService(SqlAlchemyInventoryRepository(session))
    configurations = await service.list_venues()
    return [
        VenueSummary(
            id=venue.id,
            name=venue.name,
            address=venue.address,
            timezone=venue.timezone,
            status=venue.status,
            notice=venue.notice,
            open_time=template.open_time,
            close_time=template.close_time,
            slot_minutes=template.slot_minutes,
            advance_days=template.advance_days,
            release_time=template.release_time,
            target_service_date=(
                datetime.now(ZoneInfo(venue.timezone)).date()
                + timedelta(days=template.advance_days)
            ),
            courts=[
                CourtSummary(
                    id=court.id,
                    code=court.code,
                    name=court.name,
                    status=court.status,
                )
                for court in courts
            ],
        )
        for venue, template, courts in configurations
    ]


@router.get("/{venue_id}/inventory", response_model=InventoryResponse)
async def get_inventory(
    venue_id: int,
    _principal: CurrentPrincipal,
    session: DatabaseSession,
    service_date: Annotated[date | None, Query()] = None,
) -> InventoryResponse:
    service = InventoryService(SqlAlchemyInventoryRepository(session))
    configurations = await service.list_venues()
    configuration = next((item for item in configurations if item[0].id == venue_id), None)
    if configuration is None:
        raise _not_found("场馆不存在或未开放")
    venue, template, _ = configuration
    requested_date = service_date or (
        datetime.now(ZoneInfo(venue.timezone)).date()
        + timedelta(days=template.advance_days)
    )

    try:
        view = await service.get_inventory(venue_id, requested_date)
    except InventoryNotFoundError as exc:
        raise _not_found(str(exc)) from exc

    slots_by_court: dict[int, list[InventorySlotResponse]] = {
        court.id: [] for court in view.courts
    }
    available_count = 0
    for slot, court in view.slot_rows:
        slots_by_court[court.id].append(
            InventorySlotResponse(
                id=slot.id,
                start_at=slot.start_at,
                end_at=slot.end_at,
                status=slot.status,
            )
        )
        if slot.status is BookingSlotStatus.AVAILABLE:
            available_count += 1

    return InventoryResponse(
        venue_id=view.venue.id,
        venue_name=view.venue.name,
        release_batch_id=view.batch.id,
        service_date=view.batch.service_date,
        release_at=view.batch.release_at,
        batch_status=view.batch.status,
        is_released=view.batch.status is ReleaseBatchStatus.OPEN,
        slot_count=view.batch.slot_count,
        available_count=available_count,
        courts=[
            CourtInventoryResponse(
                id=court.id,
                code=court.code,
                name=court.name,
                status=court.status,
                slots=slots_by_court[court.id],
            )
            for court in view.courts
        ],
    )


def _not_found(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "INVENTORY_NOT_FOUND", "message": message},
    )
