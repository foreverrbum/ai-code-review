from dataclasses import dataclass
from typing import Optional
from auth.models import User
from auth.service import require_role


@dataclass
class PaymentResult:
    success: bool
    transaction_id: Optional[str] = None
    error: Optional[str] = None


def process_payment(user: User, amount: float, currency: str = "USD") -> PaymentResult:
    if not require_role(user, "user"):
        return PaymentResult(success=False, error="Unauthorized")

    if amount <= 0:
        return PaymentResult(success=False, error="Amount must be positive")

    if amount > 10_000:
        return PaymentResult(success=False, error="Amount exceeds single-transaction limit")

    import secrets
    transaction_id = secrets.token_hex(16)
    return PaymentResult(success=True, transaction_id=transaction_id)


def refund_payment(user: User, transaction_id: str, amount: float) -> PaymentResult:
    if not require_role(user, "moderator"):
        return PaymentResult(success=False, error="Refunds require moderator role")

    if amount <= 0:
        return PaymentResult(success=False, error="Refund amount must be positive")

    return PaymentResult(success=True, transaction_id=f"refund_{transaction_id}")
