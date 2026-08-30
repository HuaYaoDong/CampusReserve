from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from campus_reserve.auth.dependencies import CurrentPrincipal
from campus_reserve.auth.infrastructure.sqlalchemy_repository import SqlAlchemyUserRepository
from campus_reserve.auth.schemas import LoginRequest, TokenResponse, UserProfile
from campus_reserve.auth.security import create_access_token
from campus_reserve.auth.services.authenticate import AuthService, InvalidCredentialsError
from campus_reserve.shared.database import get_session

router = APIRouter()
DatabaseSession = Annotated[AsyncSession, Depends(get_session)]


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, session: DatabaseSession) -> TokenResponse:
    service = AuthService(SqlAlchemyUserRepository(session))
    try:
        user = await service.authenticate(payload.username, payload.password)
    except InvalidCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_CREDENTIALS", "message": "用户名或密码错误"},
        ) from exc

    token, expires_in = create_access_token(user)
    return TokenResponse(
        access_token=token,
        expires_in=expires_in,
        user=UserProfile.model_validate(user),
    )


@router.get("/me", response_model=UserProfile)
async def me(principal: CurrentPrincipal, session: DatabaseSession) -> UserProfile:
    service = AuthService(SqlAlchemyUserRepository(session))
    try:
        user = await service.get_active_user(principal.user_id)
    except InvalidCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHORIZED", "message": "用户不存在或已停用"},
        ) from exc
    return UserProfile.model_validate(user)
