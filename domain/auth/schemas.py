from __future__ import annotations

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    email: str
    password: str = Field(min_length=8)
    display_name: str = ""


class LoginRequest(BaseModel):
    email: str
    password: str


class AuthUserView(BaseModel):
    id: str
    email: str
    email_verified: bool
    display_name: str
    avatar_url: str = ""


class AuthResponse(BaseModel):
    user: AuthUserView
    session_token: str | None = None
