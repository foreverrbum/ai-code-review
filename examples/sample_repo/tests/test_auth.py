import pytest
from auth.models import User
from auth.service import authenticate, verify_password, is_session_valid, hash_password


def make_user(**kwargs):
    defaults = dict(
        id=1, email="test@example.com",
        password_hash=hash_password("secret123"),
        is_active=True, failed_login_attempts=0
    )
    return User(**{**defaults, **kwargs})


class TestAuthenticate:
    def test_valid_credentials_returns_session(self):
        user = make_user()
        result = authenticate(user, "secret123")
        assert result.success is True
        assert result.session is not None
        assert result.session.user_id == user.id

    def test_wrong_password_fails(self):
        user = make_user()
        result = authenticate(user, "wrongpassword")
        assert result.success is False
        assert result.error == "Invalid password"
        assert user.failed_login_attempts == 1

    def test_disabled_account_is_rejected(self):
        user = make_user(is_active=False)
        result = authenticate(user, "secret123")
        assert result.success is False
        assert "disabled" in result.error

    def test_locked_account_is_rejected(self):
        user = make_user(failed_login_attempts=5)
        result = authenticate(user, "secret123")
        assert result.success is False
        assert "locked" in result.error

    def test_successful_login_resets_failed_attempts(self):
        user = make_user(failed_login_attempts=3)
        authenticate(user, "secret123")
        assert user.failed_login_attempts == 0


class TestVerifyPassword:
    def test_correct_password_returns_true(self):
        h = hash_password("mypassword")
        assert verify_password(h, "mypassword") is True

    def test_wrong_password_returns_false(self):
        h = hash_password("mypassword")
        assert verify_password(h, "otherpassword") is False
