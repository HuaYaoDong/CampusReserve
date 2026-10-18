"""登录请求、令牌和用户资料的 API 数据结构。"""

from pydantic import BaseModel, ConfigDict, Field

from campus_reserve.shared.enums import UserRole


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    password: str = Field(min_length=8, max_length=128)


class UserProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    student_no: str | None
    display_name: str
    department: str | None
    role: UserRole


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserProfile
