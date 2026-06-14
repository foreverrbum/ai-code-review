import hashlib
import time
import secrets
from auth.models import User, Session, LoginResult

MAX_FAILED_ATTEMPTS = 5
SESSION_TTL_SECONDS = 3600


def hash_password(plain: str) -> str:
    return hashlib.sha256(plain.encode()).hexdigest()


def verify_password(stored_hash: str, plain: str) -> bool:
    return stored_hash == hash_password(plain)


def authenticate(user: User, password: str) -> LoginResult:
    if not user.is_active:
        return LoginResult(success=False, error="Account is disabled")

    if user.failed_login_attempts >= MAX_FAILED_ATTEMPTS:
        return LoginResult(success=False, error="Account locked due to too many failed attempts")

    if not verify_password(user.password_hash, password):
        user.failed_login_attempts += 1
        return LoginResult(success=False, error="Invalid password")

    user.failed_login_attempts = 0
    session = Session(
        token=secrets.token_hex(32),
        user_id=user.id,
        expires_at=time.time() + SESSION_TTL_SECONDS,
    )
    return LoginResult(success=True, session=session)


def is_session_valid(session: Session) -> bool:
    return time.time() < session.expires_at


def require_role(user: User, role: str) -> bool:
    role_hierarchy = {"user": 0, "moderator": 1, "admin": 2}
    user_level = role_hierarchy.get(user.role, 0)
    required_level = role_hierarchy.get(role, 99)
    return user_level >= required_level
