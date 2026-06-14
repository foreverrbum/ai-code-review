from dataclasses import dataclass
from typing import Optional


@dataclass
class User:
    id: int
    email: str
    password_hash: str
    role: str = "user"
    is_active: bool = True
    failed_login_attempts: int = 0


@dataclass
class Session:
    token: str
    user_id: int
    expires_at: float
    ip_address: Optional[str] = None


@dataclass
class LoginResult:
    success: bool
    session: Optional[Session] = None
    error: Optional[str] = None
