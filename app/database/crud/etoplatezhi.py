"""CRUD operations for EtoPlatezhi payments."""

from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import EtoplatezhiPayment


logger = structlog.get_logger(__name__)


async def create_etoplatezhi_payment(
    db: AsyncSession,
    *,
    user_id: int | None,
    order_id: str,
    amount_kopeks: int,
    currency: str = 'RUB',
    description: str | None = None,
    payment_url: str | None = None,
    force_method: str | None = None,
    expires_at: datetime | None = None,
    metadata_json: dict | None = None,
) -> EtoplatezhiPayment:
    """Create an EtoPlatezhi payment record."""
    payment = EtoplatezhiPayment(
        user_id=user_id,
        order_id=order_id,
        amount_kopeks=amount_kopeks,
        currency=currency,
        description=description,
        payment_url=payment_url,
        force_method=force_method,
        expires_at=expires_at,
        metadata_json=metadata_json,
        status='pending',
        is_paid=False,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    logger.info('Created EtoPlatezhi payment', order_id=order_id, user_id=user_id)
    return payment


async def get_etoplatezhi_payment_by_order_id(db: AsyncSession, order_id: str) -> EtoplatezhiPayment | None:
    """Get payment by order_id."""
    result = await db.execute(
        select(EtoplatezhiPayment).where(EtoplatezhiPayment.order_id == order_id),
    )
    return result.scalar_one_or_none()


async def get_etoplatezhi_payment_by_id(db: AsyncSession, payment_id: int) -> EtoplatezhiPayment | None:
    """Get payment by local ID."""
    result = await db.execute(
        select(EtoplatezhiPayment).where(EtoplatezhiPayment.id == payment_id),
    )
    return result.scalar_one_or_none()


async def get_etoplatezhi_payment_by_id_for_update(db: AsyncSession, payment_id: int) -> EtoplatezhiPayment | None:
    """Get payment by local ID with FOR UPDATE lock."""
    result = await db.execute(
        select(EtoplatezhiPayment)
        .where(EtoplatezhiPayment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True),
    )
    return result.scalar_one_or_none()


async def get_etoplatezhi_payment_by_operation_id(
    db: AsyncSession,
    operation_id: str,
) -> EtoplatezhiPayment | None:
    """Get payment by EtoPlatezhi operation ID."""
    result = await db.execute(
        select(EtoplatezhiPayment).where(EtoplatezhiPayment.etoplatezhi_operation_id == operation_id),
    )
    return result.scalar_one_or_none()


async def update_etoplatezhi_payment_status(
    db: AsyncSession,
    payment: EtoplatezhiPayment,
    *,
    status: str,
    is_paid: bool = False,
    etoplatezhi_operation_id: str | None = None,
    account_token: str | None = None,
    recurring_id: str | None = None,
    callback_payload: dict | None = None,
    transaction_id: int | None = None,
) -> EtoplatezhiPayment:
    """Update payment status."""
    payment.status = status
    payment.is_paid = is_paid
    payment.updated_at = datetime.now(UTC)

    if is_paid:
        payment.paid_at = datetime.now(UTC)
    if etoplatezhi_operation_id:
        payment.etoplatezhi_operation_id = etoplatezhi_operation_id
    if account_token:
        payment.account_token = account_token
    if recurring_id:
        payment.recurring_id = recurring_id
    if callback_payload:
        payment.callback_payload = callback_payload
    if transaction_id:
        payment.transaction_id = transaction_id

    await db.commit()
    await db.refresh(payment)
    logger.info(
        'Updated EtoPlatezhi payment status',
        order_id=payment.order_id,
        status=status,
        is_paid=is_paid,
    )
    return payment


async def get_pending_etoplatezhi_payments(db: AsyncSession, user_id: int) -> list[EtoplatezhiPayment]:
    """Get pending payments for a user."""
    result = await db.execute(
        select(EtoplatezhiPayment).where(
            EtoplatezhiPayment.user_id == user_id,
            EtoplatezhiPayment.status == 'pending',
            EtoplatezhiPayment.is_paid == False,  # noqa: E712
        ),
    )
    return list(result.scalars().all())


async def get_user_etoplatezhi_payments(
    db: AsyncSession,
    user_id: int,
    limit: int = 10,
    offset: int = 0,
) -> list[EtoplatezhiPayment]:
    """Get user's payments with pagination."""
    result = await db.execute(
        select(EtoplatezhiPayment)
        .where(EtoplatezhiPayment.user_id == user_id)
        .order_by(EtoplatezhiPayment.created_at.desc())
        .limit(limit)
        .offset(offset),
    )
    return list(result.scalars().all())


async def get_expired_pending_etoplatezhi_payments(db: AsyncSession) -> list[EtoplatezhiPayment]:
    """Get expired payments still in pending status."""
    now = datetime.now(UTC)
    result = await db.execute(
        select(EtoplatezhiPayment).where(
            EtoplatezhiPayment.status == 'pending',
            EtoplatezhiPayment.is_paid == False,  # noqa: E712
            EtoplatezhiPayment.expires_at < now,
        ),
    )
    return list(result.scalars().all())


async def get_latest_token_for_user(db: AsyncSession, user_id: int) -> dict[str, str] | None:
    """Get the most recent account token for a user from successful payments.

    Returns:
        Dict with 'account_token' and optionally 'recurring_id', or None.
    """
    result = await db.execute(
        select(EtoplatezhiPayment)
        .where(
            EtoplatezhiPayment.user_id == user_id,
            EtoplatezhiPayment.is_paid == True,  # noqa: E712
            EtoplatezhiPayment.account_token.isnot(None),
            EtoplatezhiPayment.account_token != '',
        )
        .order_by(EtoplatezhiPayment.paid_at.desc())
        .limit(1),
    )
    payment = result.scalar_one_or_none()
    if not payment:
        return None

    token_data: dict[str, str] = {'account_token': payment.account_token}
    if payment.recurring_id:
        token_data['recurring_id'] = payment.recurring_id
    return token_data
