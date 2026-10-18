"""认证仓储协议，供服务层依赖而不绑定具体数据库实现。"""

from typing import Protocol

from campus_reserve.users.models import User


class UserRepository(Protocol):
    async def get_by_username(self, username: str) -> User | None: ...

    async def get_by_id(self, user_id: int) -> User | None: ...

    async def record_login(self, user: User) -> None: ...
