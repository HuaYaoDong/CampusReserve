from typing import Protocol

from campus_reserve.users.models import User


class UserRepository(Protocol):
    async def get_by_username(self, username: str) -> User | None: ...

    async def get_by_id(self, user_id: int) -> User | None: ...

    async def record_login(self, user: User) -> None: ...
