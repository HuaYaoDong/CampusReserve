import pytest

from campus_reserve.auth.security import (
    TokenValidationError,
    create_access_token,
    decode_access_token,
    verify_password,
)
from campus_reserve.shared.config import Settings
from campus_reserve.shared.enums import UserRole, UserStatus
from campus_reserve.users.models import User


def test_password_and_access_token_round_trip() -> None:
    settings = Settings(
        jwt_secret="unit-test-secret-with-at-least-32-bytes",
        jwt_issuer="campus-reserve-tests",
        jwt_access_token_minutes=5,
    )
    user = User(
        id=42,
        username="student001",
        password_hash=(
            "$argon2id$v=19$m=65536,t=3,p=4$ZsghkEC/sljcgoGkhtMlXQ$"
            "lWZlCnrd8Xts2APLz2PiMzf+3FJFycSrxRMvMWIOinw"
        ),
        student_no="20260001",
        display_name="演示学生一",
        role=UserRole.STUDENT,
        status=UserStatus.ACTIVE,
    )

    assert verify_password("Student@123", user.password_hash)
    assert not verify_password("WrongPassword", user.password_hash)

    token, expires_in = create_access_token(user, settings)
    principal = decode_access_token(token, settings)

    assert expires_in == 300
    assert principal.user_id == 42
    assert principal.username == "student001"
    assert principal.role is UserRole.STUDENT


def test_access_token_rejects_a_different_secret() -> None:
    issuer = "campus-reserve-tests"
    user = User(
        id=42,
        username="student001",
        password_hash="unused",
        student_no="20260001",
        display_name="演示学生一",
        role=UserRole.STUDENT,
        status=UserStatus.ACTIVE,
    )
    token, _ = create_access_token(
        user,
        Settings(jwt_secret="correct-unit-test-secret-at-least-32-bytes", jwt_issuer=issuer),
    )

    with pytest.raises(TokenValidationError):
        decode_access_token(
            token,
            Settings(jwt_secret="wrong-unit-test-secret-at-least-32-bytes", jwt_issuer=issuer),
        )
