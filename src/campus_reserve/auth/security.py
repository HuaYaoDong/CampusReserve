"""密码哈希和 JWT 签发验证；不得在接口层直接处理明文密码。"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from jwt import InvalidTokenError
from pwdlib import PasswordHash

from campus_reserve.shared.config import Settings, get_settings
from campus_reserve.shared.enums import UserRole
from campus_reserve.users.models import User

password_hash = PasswordHash.recommended()
DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$ZsghkEC/sljcgoGkhtMlXQ$"
    "lWZlCnrd8Xts2APLz2PiMzf+3FJFycSrxRMvMWIOinw"
)


@dataclass(frozen=True, slots=True)
class AuthPrincipal:
    user_id: int
    username: str
    role: UserRole


class TokenValidationError(ValueError):
    pass


def verify_password(password: str, encoded_hash: str) -> bool:
    return password_hash.verify(password, encoded_hash)


# JWT 只携带必要身份声明并设置有限有效期。
def create_access_token(user: User, settings: Settings | None = None) -> tuple[str, int]:
    active_settings = settings or get_settings()
    now = datetime.now(UTC)
    expires_in = active_settings.jwt_access_token_minutes * 60
    payload: dict[str, Any] = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role.value,
        "iss": active_settings.jwt_issuer,
        "iat": now,
        "exp": now + timedelta(seconds=expires_in),
    }
    token = jwt.encode(payload, active_settings.jwt_secret, algorithm=active_settings.jwt_algorithm)
    return token, expires_in


# 校验签名、签发方和过期时间，不只相信令牌中的角色文本。
def decode_access_token(token: str, settings: Settings | None = None) -> AuthPrincipal:
    active_settings = settings or get_settings()
    try:
        payload = jwt.decode(
            token,
            active_settings.jwt_secret,
            algorithms=[active_settings.jwt_algorithm],
            issuer=active_settings.jwt_issuer,
            options={"require": ["sub", "username", "role", "iat", "exp", "iss"]},
        )
        return AuthPrincipal(
            user_id=int(payload["sub"]),
            username=str(payload["username"]),
            role=UserRole(payload["role"]),
        )
    except (InvalidTokenError, KeyError, TypeError, ValueError) as exc:
        raise TokenValidationError("Invalid or expired access token") from exc
