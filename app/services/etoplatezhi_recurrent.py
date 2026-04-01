"""Recurrent (auto) payments via EtoPlatezhi.

Handles:
- Registering recurring on first payment (type=R, period=M via Payment Page)
- Auto-charging existing subscribers when balance is low
- Cancelling recurring subscriptions

Follows the same pattern as recurrent_payment_service.py (YooKassa)
but adapted for EtoPlatezhi Gate API (token-based recurring charges).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.models import (
    Subscription,
    SubscriptionStatus,
    User,
    UserPromoGroup,
)


logger = structlog.get_logger(__name__)


@dataclass
class _DailyGuard:
    """Prevents re-processing the same subscription within one calendar day."""

    date: str = ''
    processed: set[str] = field(default_factory=set)

    def reset_if_new_day(self) -> None:
        today = datetime.now(UTC).strftime('%Y-%m-%d')
        if today != self.date:
            self.processed = set()
            self.date = today

    def is_processed(self, key: str) -> bool:
        return key in self.processed

    def mark_processed(self, key: str) -> None:
        self.processed.add(key)


_daily_guard = _DailyGuard()


def _build_extend_keyboard(texts: Any, subscription_id: int | None = None) -> InlineKeyboardMarkup:
    """Keyboard with subscription renewal button for notifications."""
    extend_callback = (
        f'se:{subscription_id}' if settings.is_multi_tariff_enabled() and subscription_id else 'subscription_extend'
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('SUBSCRIPTION_EXTEND', '💎 Продлить подписку'),
                    callback_data=extend_callback,
                )
            ],
        ]
    )


async def process_etoplatezhi_recurrent_payments(db: AsyncSession, bot: Bot | None = None) -> dict:
    """Find subscriptions needing renewal and charge via EtoPlatezhi recurring.

    This function should be called from the monitoring cycle, just like
    process_recurrent_payments() for YooKassa.

    Args:
        db: DB session from the calling code (_monitoring_cycle).
        bot: Bot instance for user notifications.

    Returns:
        Processing statistics dict.
    """
    if not getattr(settings, 'ETOPLATEZHI_RECURRENT_ENABLED', False):
        return {'skipped': True, 'reason': 'etoplatezhi_recurrent_disabled'}

    if not settings.is_etoplatezhi_enabled():
        return {'skipped': True, 'reason': 'etoplatezhi_disabled'}

    if not settings.ENABLE_AUTOPAY:
        return {'skipped': True, 'reason': 'autopay_disabled'}

    _daily_guard.reset_if_new_day()

    stats = {
        'checked': 0,
        'payments_created': 0,
        'no_token': 0,
        'charge_failed': 0,
        'already_processed': 0,
        'errors': 0,
    }

    from app.services.payment_service import PaymentService
    from app.services.subscription_service import SubscriptionService

    payment_service = PaymentService()
    subscription_service = SubscriptionService()

    try:
        subscriptions = await _find_subscriptions_needing_topup(db)
        stats['checked'] = len(subscriptions)

        for subscription in subscriptions:
            user = subscription.user
            if not user:
                continue

            guard_key = f'ep_{user.id}_{subscription.id}'
            if _daily_guard.is_processed(guard_key):
                stats['already_processed'] += 1
                continue

            try:
                result = await _process_single_subscription(
                    db,
                    subscription,
                    user,
                    bot,
                    payment_service,
                )
                if result == 'created':
                    stats['payments_created'] += 1
                    _daily_guard.mark_processed(guard_key)
                elif result == 'no_token':
                    stats['no_token'] += 1
                    _daily_guard.mark_processed(guard_key)
                elif result == 'charge_failed':
                    stats['charge_failed'] += 1
                    _daily_guard.mark_processed(guard_key)
                elif result == 'skipped':
                    stats['already_processed'] += 1
            except Exception as e:
                stats['errors'] += 1
                logger.error(
                    'EtoPlatezhi recurrent: error processing subscription',
                    subscription_id=subscription.id,
                    user_id=user.id,
                    error=e,
                    exc_info=True,
                )
    except Exception as e:
        logger.error('EtoPlatezhi recurrent: error fetching subscriptions', error=e, exc_info=True)
        stats['errors'] += 1

    if stats['payments_created'] > 0 or stats['errors'] > 0:
        logger.info('EtoPlatezhi recurrent: summary', **stats)

    return stats


async def _find_subscriptions_needing_topup(db: AsyncSession) -> list:
    """Find subscriptions with autopay that need renewal soon."""
    current_time = datetime.now(UTC)
    max_days_before = settings.DEFAULT_AUTOPAY_DAYS_BEFORE
    check_horizon = current_time + timedelta(days=max_days_before + 1)
    recently_expired_threshold = current_time - timedelta(hours=48)

    result = await db.execute(
        select(Subscription)
        .options(
            selectinload(Subscription.user).options(
                selectinload(User.promo_group),
                selectinload(User.user_promo_groups).selectinload(UserPromoGroup.promo_group),
            ),
            selectinload(Subscription.tariff),
        )
        .where(
            and_(
                or_(
                    and_(
                        Subscription.status == SubscriptionStatus.ACTIVE.value,
                        Subscription.end_date <= check_horizon,
                    ),
                    and_(
                        Subscription.status == SubscriptionStatus.EXPIRED.value,
                        Subscription.end_date >= recently_expired_threshold,
                    ),
                ),
                Subscription.autopay_enabled == True,  # noqa: E712
                Subscription.is_trial == False,  # noqa: E712
            )
        )
    )
    return list(result.scalars().all())


async def _process_single_subscription(
    db: AsyncSession,
    subscription: Subscription,
    user: User,
    bot: Bot | None,
    payment_service: Any,
) -> str:
    """Process one subscription: check balance, find token, charge.

    Returns:
        'created' — charge initiated successfully.
        'no_token' — no saved EtoPlatezhi token.
        'charge_failed' — charge request failed.
        'skipped' — balance is sufficient or other skip reason.
    """
    # Calculate renewal cost
    tariff = getattr(subscription, 'tariff', None)
    if tariff:
        autopay_period = tariff.get_shortest_period() or 30
    else:
        autopay_period = 30

    try:
        from app.database.crud.user import lock_user_for_pricing
        from app.services.pricing_engine import pricing_engine

        user = await lock_user_for_pricing(db, user.id)

        pricing = await pricing_engine.calculate_renewal_price(
            db,
            subscription,
            autopay_period,
            user=user,
        )
        renewal_cost = pricing.final_total
    except Exception as e:
        logger.error(
            'EtoPlatezhi recurrent: pricing error',
            subscription_id=subscription.id,
            user_id=user.id,
            error=e,
        )
        return 'skipped'

    if renewal_cost <= 0:
        return 'skipped'

    # Check if balance is sufficient
    shortage = renewal_cost - user.balance_kopeks
    if shortage <= 0:
        return 'skipped'

    # Check if within autopay window
    days_before = getattr(subscription, 'autopay_days_before', None) or settings.DEFAULT_AUTOPAY_DAYS_BEFORE
    days_until_expiry = (subscription.end_date - datetime.now(UTC)).total_seconds() / 86400
    if days_until_expiry > days_before and subscription.status != SubscriptionStatus.EXPIRED.value:
        return 'skipped'

    # Find saved EtoPlatezhi token for this user
    token = await _get_user_etoplatezhi_token(db, user.id)
    if not token:
        # Notify user that no payment method is saved
        if bot and user.telegram_id:
            try:
                from app.localization.texts import get_texts

                texts = get_texts(user.language)
                keyboard = _build_extend_keyboard(texts, subscription.id)
                msg = texts.t(
                    'RECURRENT_NO_SAVED_METHOD',
                    '⚠️ <b>Автоплатеж невозможен</b>\n\n'
                    'Нет сохранённого способа оплаты для автопродления подписки.\n'
                    'Пополните баланс вручную.',
                )
                if settings.is_multi_tariff_enabled() and tariff:
                    msg += f'\n📦 Тариф: «{tariff.name}»'
                await bot.send_message(
                    chat_id=user.telegram_id,
                    text=msg,
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
            except Exception as notify_err:
                logger.warning('EtoPlatezhi recurrent: notification error', error=notify_err)
        return 'no_token'

    # Calculate topup amount
    min_amount = getattr(settings, 'ETOPLATEZHI_MIN_AMOUNT_KOPEKS', 10000)
    topup_amount_kopeks = max(shortage, min_amount)

    # Generate unique payment_id for this charge
    today = datetime.now(UTC).strftime('%Y%m%d')
    payment_id = f'epr_{user.id}_{subscription.id}_{today}_{uuid.uuid4().hex[:4]}'

    try:
        from app.services.etoplatezhi_service import etoplatezhi_service

        result = await etoplatezhi_service.charge_recurrent(
            payment_id=payment_id,
            token=token['account_token'],
            amount=topup_amount_kopeks,
            currency=getattr(settings, 'ETOPLATEZHI_CURRENCY', 'RUB'),
            customer_id=str(user.telegram_id or user.id),
            scheduled_payment_id=token.get('recurring_id'),
        )

        if not result:
            logger.warning(
                'EtoPlatezhi recurrent: charge returned empty result',
                user_id=user.id,
                subscription_id=subscription.id,
            )
            return 'charge_failed'

        # Check if charge was successful
        resp_status = result.get('payment', {}).get('status', '')
        if resp_status in ('success', 'processing'):
            logger.info(
                'EtoPlatezhi recurrent: charge initiated',
                user_id=user.id,
                subscription_id=subscription.id,
                amount_kopeks=topup_amount_kopeks,
                payment_id=payment_id,
                status=resp_status,
            )

            # Save local payment record
            try:
                from importlib import import_module

                etoplatezhi_crud = import_module('app.database.crud.etoplatezhi')
                await etoplatezhi_crud.create_etoplatezhi_payment(
                    db=db,
                    user_id=user.id,
                    order_id=payment_id,
                    amount_kopeks=topup_amount_kopeks,
                    currency=getattr(settings, 'ETOPLATEZHI_CURRENCY', 'RUB'),
                    description=f'Автоплатеж EtoPlatezhi (подписка #{subscription.id})',
                    metadata_json={
                        'user_id': user.id,
                        'amount_kopeks': topup_amount_kopeks,
                        'type': 'recurrent_topup',
                        'subscription_id': subscription.id,
                        'source': 'etoplatezhi_recurrent',
                    },
                )
            except Exception as crud_err:
                logger.warning('EtoPlatezhi recurrent: error saving local record', error=crud_err)

            # Notify user
            if bot and user.telegram_id and resp_status == 'success':
                try:
                    from app.localization.texts import get_texts

                    texts = get_texts(user.language)
                    keyboard = _build_extend_keyboard(texts, subscription.id)
                    msg = texts.t(
                        'RECURRENT_TOPUP_SUCCESS',
                        '✅ <b>Автоплатёж выполнен</b>\n\n'
                        'Баланс пополнен на {amount} для продления подписки.',
                    ).format(amount=settings.format_price(topup_amount_kopeks))
                    if settings.is_multi_tariff_enabled() and tariff:
                        msg += f'\n📦 Тариф: «{tariff.name}»'
                    await bot.send_message(
                        chat_id=user.telegram_id,
                        text=msg,
                        parse_mode='HTML',
                        reply_markup=keyboard,
                    )
                except Exception as notify_err:
                    logger.warning('EtoPlatezhi recurrent: notification error', error=notify_err)

            return 'created'

        # Charge declined or failed
        logger.warning(
            'EtoPlatezhi recurrent: charge declined',
            user_id=user.id,
            subscription_id=subscription.id,
            status=resp_status,
            result=result,
        )

    except Exception as e:
        logger.error(
            'EtoPlatezhi recurrent: charge error',
            user_id=user.id,
            subscription_id=subscription.id,
            error=e,
            exc_info=True,
        )

    # Notify user about failed charge
    if bot and user.telegram_id:
        try:
            from app.localization.texts import get_texts

            texts = get_texts(user.language)
            keyboard = _build_extend_keyboard(texts, subscription.id)
            msg = texts.t(
                'RECURRENT_TOPUP_FAILED',
                '❌ <b>Автоплатёж не удался</b>\n\n'
                'Не удалось списать {amount} для продления подписки.\n\n'
                'Пополните баланс вручную, чтобы подписка не прервалась.',
            ).format(amount=settings.format_price(topup_amount_kopeks))
            if settings.is_multi_tariff_enabled() and tariff:
                msg += f'\n📦 Тариф: «{tariff.name}»'
            await bot.send_message(
                chat_id=user.telegram_id,
                text=msg,
                parse_mode='HTML',
                reply_markup=keyboard,
            )
        except Exception as notify_err:
            logger.warning('EtoPlatezhi recurrent: failed charge notification error', error=notify_err)

    return 'charge_failed'


async def _get_user_etoplatezhi_token(db: AsyncSession, user_id: int) -> dict[str, str] | None:
    """Find the most recent EtoPlatezhi account token for a user.

    Looks in etoplatezhi_payments table for a successful payment that has
    an account_token saved from the callback.

    Returns:
        Dict with 'account_token' and optionally 'recurring_id', or None.
    """
    try:
        from app.database.crud.etoplatezhi import get_latest_token_for_user

        return await get_latest_token_for_user(db, user_id)
    except Exception as e:
        logger.error('EtoPlatezhi: error getting user token', user_id=user_id, error=e)
        return None


async def cancel_etoplatezhi_recurring(recurring_id: str) -> bool:
    """Cancel an EtoPlatezhi recurring subscription.

    Args:
        recurring_id: Recurring series ID from the registration callback.

    Returns:
        True if cancellation was successful.
    """
    try:
        from app.services.etoplatezhi_service import etoplatezhi_service

        result = await etoplatezhi_service.cancel_recurring(recurring_id=recurring_id)

        # Check result
        status = result.get('status', '')
        if status in ('success', 'cancelled'):
            logger.info('EtoPlatezhi recurring cancelled', recurring_id=recurring_id)
            return True

        logger.warning(
            'EtoPlatezhi recurring cancellation: unexpected status',
            recurring_id=recurring_id,
            status=status,
            result=result,
        )
        return False

    except Exception as e:
        logger.exception('EtoPlatezhi: error cancelling recurring', recurring_id=recurring_id, error=e)
        return False
