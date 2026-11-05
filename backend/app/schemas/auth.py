from datetime import datetime
import re
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


class UserRegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=32, description="用户名 (3-32位字母、数字、下划线或连字符)")
    password: str = Field(..., min_length=6, max_length=128, description="用户密码 (至少6位)")
    email: Optional[str] = Field(None, max_length=128, description="电子邮箱 (可选)")

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        cleaned = v.strip()
        if not re.match(r"^[a-zA-Z0-9_\-]+$", cleaned):
            raise ValueError("用户名仅支持包含英文字母、数字、下划线 (_) 或连字符 (-)。")
        return cleaned

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        cleaned = v.strip().lower()
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", cleaned):
            raise ValueError("电子邮箱格式不正确。")
        return cleaned


class UserLoginRequest(BaseModel):
    username: str = Field(..., description="用户名或邮箱")
    password: str = Field(..., description="登录密码")

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        return v.strip()


class UserResponse(BaseModel):
    id: str
    username: str
    email: Optional[str] = None
    role: str
    is_active: bool
    created_at: datetime
    last_login_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class TokenResponse(BaseModel):
    access_token: str = Field(..., description="HMAC-SHA256 签名的 Bearer Token")
    token_type: str = Field(default="bearer", description="Token 类型")
    expires_in: int = Field(default=86400, description="有效秒数 (默认 86400 秒 = 24 小时)")
    user: UserResponse = Field(..., description="当前登录用户信息")


class AuthModeResponse(BaseModel):
    deployment_mode: str = Field(..., description="系统部署模式: local 或 server")
    allow_registration: bool = Field(..., description="是否允许公开注册新账号")
    version: str = Field(default="1.3.0", description="系统版本")


class LogoutResponse(BaseModel):
    message: str = Field(default="登出成功，请丢弃客户端本地 Token。")
    revocation_type: str = Field(
        default="client_discard",
        description="退出撤销机制说明：client_discard 表示客户端丢弃 Token；若需服务端强制全局即时吊销所有设备凭证，请调用修改密码接口触发 token_version 自增。",
    )


class PasswordChangeRequest(BaseModel):
    old_password: str = Field(..., description="当前原密码")
    new_password: str = Field(..., min_length=6, max_length=128, description="新密码 (至少6位)")


class PasswordChangeResponse(BaseModel):
    message: str = Field(default="密码修改成功，所有设备既有会话已即时失效，请使用新密码重新登录。")
