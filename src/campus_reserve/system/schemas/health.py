from typing import Literal

from pydantic import BaseModel


class LiveResponse(BaseModel):
    status: Literal["ok"]


class ComponentHealth(BaseModel):
    status: Literal["ok", "error"]
    detail: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    database: ComponentHealth
    redis: ComponentHealth

