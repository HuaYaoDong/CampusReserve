"""FastAPI 身份依赖：解码令牌并限制学生或管理员角色。"""

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from campus_reserve.auth.security import AuthPrincipal, TokenValidationError, decode_access_token

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> AuthPrincipal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()
    try:
        return decode_access_token(credentials.credentials)
    except TokenValidationError as exc:
        raise _unauthorized() from exc


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "UNAUTHORIZED", "message": "登录状态无效或已过期"},
        headers={"WWW-Authenticate": "Bearer"},
    )


CurrentPrincipal = Annotated[AuthPrincipal, Depends(get_current_principal)]
