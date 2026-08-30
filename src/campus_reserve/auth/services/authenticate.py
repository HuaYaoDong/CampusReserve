from campus_reserve.auth.repositories import UserRepository
from campus_reserve.auth.security import DUMMY_PASSWORD_HASH, verify_password
from campus_reserve.shared.enums import UserStatus
from campus_reserve.users.models import User


class InvalidCredentialsError(ValueError):
    pass


class AuthService:
    def __init__(self, repository: UserRepository) -> None:
        self.repository = repository

    async def authenticate(self, username: str, password: str) -> User:
        user = await self.repository.get_by_username(username.strip())
        encoded_hash = user.password_hash if user else DUMMY_PASSWORD_HASH
        password_valid = verify_password(password, encoded_hash)

        if user is None or not password_valid or user.status is not UserStatus.ACTIVE:
            raise InvalidCredentialsError

        await self.repository.record_login(user)
        return user

    async def get_active_user(self, user_id: int) -> User:
        user = await self.repository.get_by_id(user_id)
        if user is None or user.status is not UserStatus.ACTIVE:
            raise InvalidCredentialsError
        return user
