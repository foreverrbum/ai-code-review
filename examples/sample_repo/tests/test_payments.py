import pytest
from auth.models import User
from auth.service import hash_password
from payments.processor import process_payment, refund_payment


def make_user(role="user"):
    return User(id=1, email="test@example.com", password_hash=hash_password("x"), role=role)


class TestProcessPayment:
    def test_valid_payment_succeeds(self):
        user = make_user()
        result = process_payment(user, 100.0)
        assert result.success is True
        assert result.transaction_id is not None

    def test_zero_amount_rejected(self):
        user = make_user()
        result = process_payment(user, 0)
        assert result.success is False

    def test_negative_amount_rejected(self):
        user = make_user()
        result = process_payment(user, -50)
        assert result.success is False

    def test_exceeds_limit_rejected(self):
        user = make_user()
        result = process_payment(user, 15_000)
        assert result.success is False


class TestRefundPayment:
    def test_moderator_can_refund(self):
        user = make_user(role="moderator")
        result = refund_payment(user, "txn_abc123", 50.0)
        assert result.success is True

    def test_regular_user_cannot_refund(self):
        user = make_user(role="user")
        result = refund_payment(user, "txn_abc123", 50.0)
        assert result.success is False
        assert "moderator" in result.error
