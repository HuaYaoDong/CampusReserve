from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from campus_reserve.auth.repositories import UserRepository
from campus_reserve.users.models import User


class SqlAlchemyUserRepository(UserRepository):
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_username(self, username: str) -> User | None:
        statement = select(User).where(func.lower(User.username) == username.lower())
        return await self.session.scalar(statement)

    async def get_by_id(self, user_id: int) -> User | None:
        return await self.session.get(User, user_id)

    async def record_login(self, user: User) -> None:
        user.last_login_at = datetime.now(UTC)
        await self.session.flush()
