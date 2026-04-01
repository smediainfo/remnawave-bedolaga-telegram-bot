"""Mixin for EtoPlatezhi payment integration (paymentpage.etoplatezhi.ru)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.etoplatezhi_service import (
    ETOPLATEZHI_METHODS,
    etoplatezhi_service,
)
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


class EtoplatezhiPaymentMixin:
    """Mixin for processing EtoPlatezhi payments."""

    async def create_etoplatezhi_payment(
        self,
        db: AsyncSession,
        *,
        user_id: int | None,
        amount_kopeks: int,
        description: str = 'Пополнение баланса',
        language: str = 'ru',
        force_method: str | None = None,
    ) -> dict[str, Any] | None:
        """Create an EtoPlatezhi payment via Payment Page redirect.

        Args:
            db: DB session.
            user_id: User ID (None for guest payments).
            amount_kopeks: Amount in kopeks.
            description: Payment description.
            language: Form language code.
            force_method: One of 'card', 'sberpay', 'tpay', 'sbp' or None.

        Returns:
            Dict with payment data (payment_url, order_id, ...) or None on error.
        """
        if not settings.is_etoplatezhi_enabled():
            logger.error('EtoPlatezhi is not configured')
            return None

        # Validate limits
        min_amount = getattr(settings, 'ETOPLATEZHI_MIN_AMOUNT_KOPEKS', 10000)
        max_amount = getattr(settings, 'ETOPLATEZHI_MAX_AMOUNT_KOPEKS', 100_000_000)

        if amount_kopeks < min_amount:
            logger.warning(
                'EtoPlatezhi: amount below minimum',
                amount_kopeks=amount_kopeks,
                min_amount=min_amount,
            )
            return None

        if amount_kopeks > max_amount:
            logger.warning(
                'EtoPlatezhi: amount above maximum',
                amount_kopeks=amount_kopeks,
                max_amount=max_amount,
            )
            return None

        # Resolve telegram_id for order_id prefix
        payment_module = import_module('app.services.payment_service')
        if user_id is not None:
            user = await payment_module.get_user_by_id(db, user_id)
        else:
            user = None
        tg_id = (user.telegram_id or user.id) if user else (user_id or 'guest')

        # Unique order ID
        order_id = f'ep{tg_id}_{uuid.uuid4().hex[:6]}'
        currency = getattr(settings, 'ETOPLATEZHI_CURRENCY', 'RUB')
        expires_at = datetime.now(UTC) + timedelta(hours=1)

        # Resolve force_payment_method code
        force_payment_method: str | None = None
        if force_method and force_method in ETOPLATEZHI_METHODS:
            force_payment_method = ETOPLATEZHI_METHODS[force_method]

        # Check if recurrent should be registered on first payment
        recurrent_enabled = getattr(settings, 'ETOPLATEZHI_RECURRENT_ENABLED', False)

        metadata = {
            'user_id': user_id,
            'amount_kopeks': amount_kopeks,
            'description': description,
            'language': language,
            'type': 'balance_topup',
            'force_method': force_method,
        }

        try:
            payment_url = etoplatezhi_service.build_payment_url(
                payment_id=order_id,
                amount=amount_kopeks,
                currency=currency,
                customer_id=str(tg_id),
                description=description,
                force_payment_method=force_payment_method,
                language_code=language,
                recurring_register=recurrent_enabled,
                recurring_type='R' if recurrent_enabled else None,
                recurring_period='M' if recurrent_enabled else None,
                recurring_interval=1 if recurrent_enabled else None,
                recurring_amount=amount_kopeks if recurrent_enabled else None,
            )

            if not payment_url:
                logger.error('EtoPlatezhi: failed to build payment URL')
                return None

            logger.info(
                'EtoPlatezhi: created payment URL',
                order_id=order_id,
                amount_kopeks=amount_kopeks,
            )

            # Save to DB
            etoplatezhi_crud = import_module('app.database.crud.etoplatezhi')

            local_payment = await etoplatezhi_crud.create_etoplatezhi_payment(
                db=db,
                user_id=user_id,
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                currency=currency,
                description=description,
                payment_url=payment_url,
                force_method=force_method,
                expires_at=expires_at,
                metadata_json=metadata,
            )

            amount_rubles = amount_kopeks / 100

            return {
                'order_id': order_id,
                'amount_kopeks': amount_kopeks,
                'amount_rubles': amount_rubles,
                'currency': currency,
                'payment_url': payment_url,
                'expires_at': expires_at.isoformat(),
                'local_payment_id': local_payment.id,
            }

        except Exception as e:
            logger.exception('EtoPlatezhi: error creating payment', e=e)
            return None

    async def process_etoplatezhi_webhook(
        self,
        db: AsyncSession,
        *,
        callback_data: dict[str, Any],
    ) -> bool:
        """Process incoming callback from EtoPlatezhi.

        Callback contains nested structures:
        - payment.id, payment.status, payment.type
        - operation.id, operation.type, operation.status
        - account (card info, token)
        - customer (id, ip)
        - signature

        Args:
            db: DB session.
            callback_data: Full callback JSON body.

        Returns:
            True if payment was successfully processed.
        """
        try:
            # Verify signature
            if not etoplatezhi_service.verify_callback(callback_data):
                logger.warning(
                    'EtoPlatezhi webhook: invalid signature',
                    payment_id=callback_data.get('payment', {}).get('id'),
                )
                return False

            # Extract fields from nested structure
            payment_data = callback_data.get('payment', {})
            operation_data = callback_data.get('operation', {})
            account_data = callback_data.get('account', {})
            recurring_data = callback_data.get('recurring', {})

            payment_id = str(payment_data.get('id', ''))
            payment_status = str(payment_data.get('status', ''))
            operation_id = str(operation_data.get('id', ''))
            operation_type = str(operation_data.get('type', ''))
            operation_status = str(operation_data.get('status', ''))

            logger.info(
                'EtoPlatezhi webhook received',
                payment_id=payment_id,
                payment_status=payment_status,
                operation_type=operation_type,
                operation_status=operation_status,
            )

            # We only finalize on successful sale operations
            if operation_type not in ('sale', 'payment') or operation_status != 'success':
                logger.info(
                    'EtoPlatezhi webhook: non-final operation, skipping finalization',
                    operation_type=operation_type,
                    operation_status=operation_status,
                )
                # Still return True to acknowledge receipt
                return True

            # Look up local payment
            etoplatezhi_crud = import_module('app.database.crud.etoplatezhi')

            payment = await etoplatezhi_crud.get_etoplatezhi_payment_by_order_id(db, payment_id)
            if not payment:
                logger.warning('EtoPlatezhi webhook: payment not found', payment_id=payment_id)
                return False

            # Lock row to prevent concurrent processing
            locked = await etoplatezhi_crud.get_etoplatezhi_payment_by_id_for_update(db, payment.id)
            if not locked:
                logger.error('EtoPlatezhi webhook: failed to lock payment', payment_id=payment.id)
                return False
            payment = locked

            if payment.is_paid:
                logger.info('EtoPlatezhi webhook: payment already processed', order_id=payment_id)
                return True

            # Verify amount (payment_amount is in minor units, same as our kopeks)
            callback_amount = payment_data.get('sum')
            if callback_amount is not None:
                try:
                    callback_amount_int = int(callback_amount)
                    if callback_amount_int != payment.amount_kopeks:
                        logger.warning(
                            'EtoPlatezhi webhook: amount mismatch',
                            expected=payment.amount_kopeks,
                            received=callback_amount_int,
                        )
                        return False
                except (ValueError, TypeError):
                    pass

            # Update payment record
            callback_payload = {
                'payment': payment_data,
                'operation': operation_data,
                'account': account_data,
            }

            payment.status = 'success'
            payment.is_paid = True
            payment.paid_at = datetime.now(UTC)
            payment.callback_payload = callback_payload
            payment.etoplatezhi_operation_id = operation_id
            payment.updated_at = datetime.now(UTC)

            # Save account token for recurrent payments
            account_token = account_data.get('token')
            if account_token:
                payment.account_token = account_token

            # Save recurring ID if present
            recurring_id = recurring_data.get('id')
            if recurring_id:
                payment.recurring_id = str(recurring_id)

            await db.flush()

            # Finalize (credit balance, create transaction, notify)
            return await self._finalize_etoplatezhi_payment(
                db, payment, operation_id=operation_id, trigger='webhook',
            )

        except Exception as e:
            logger.exception('EtoPlatezhi webhook: processing error', e=e)
            return False

    async def _finalize_etoplatezhi_payment(
        self,
        db: AsyncSession,
        payment: Any,
        *,
        operation_id: str | None,
        trigger: str,
    ) -> bool:
        """Create transaction, credit balance, send notifications."""
        payment_module = import_module('app.services.payment_service')

        # Idempotency check
        if payment.transaction_id:
            logger.info(
                'EtoPlatezhi payment already linked to transaction',
                order_id=payment.order_id,
                trigger=trigger,
            )
            return True

        # --- Guest purchase flow ---
        ep_metadata = dict(getattr(payment, 'metadata_json', {}) or {})
        from app.services.payment.common import try_fulfill_guest_purchase

        guest_result = await try_fulfill_guest_purchase(
            db,
            metadata=ep_metadata,
            payment_amount_kopeks=payment.amount_kopeks,
            provider_payment_id=str(operation_id) if operation_id else payment.order_id,
            provider_name='etoplatezhi',
        )
        if guest_result is not None:
            return True

        # Get user
        user = await payment_module.get_user_by_id(db, payment.user_id)
        if not user:
            logger.error(
                'User not found for EtoPlatezhi payment',
                user_id=payment.user_id,
                order_id=payment.order_id,
                trigger=trigger,
            )
            return False

        # Create transaction
        transaction = await payment_module.create_transaction(
            db,
            user_id=payment.user_id,
            type=TransactionType.DEPOSIT,
            amount_kopeks=payment.amount_kopeks,
            description=f'Пополнение через EtoPlatezhi (#{operation_id or payment.order_id})',
            payment_method=PaymentMethod.ETOPLATEZHI,
            external_id=str(operation_id) if operation_id else payment.order_id,
            is_completed=True,
            created_at=getattr(payment, 'created_at', None),
            commit=False,
        )

        # Link payment to transaction
        payment.transaction_id = transaction.id
        payment.updated_at = datetime.now(UTC)
        await db.flush()

        # Lock user row for balance update
        from app.database.crud.user import lock_user_for_update

        user = await lock_user_for_update(db, user)

        old_balance = user.balance_kopeks
        was_first_topup = not user.has_made_first_topup

        # Credit balance
        user.balance_kopeks += payment.amount_kopeks
        user.updated_at = datetime.now(UTC)

        promo_group = user.get_primary_promo_group()
        subscription = getattr(user, 'subscription', None)
        referrer_info = format_referrer_info(user)
        topup_status = 'Первое пополнение' if was_first_topup else 'Пополнение'

        await db.commit()

        # Emit deferred side-effects after atomic commit
        from app.database.crud.transaction import emit_transaction_side_effects

        await emit_transaction_side_effects(
            db,
            transaction,
            amount_kopeks=payment.amount_kopeks,
            user_id=payment.user_id,
            type=TransactionType.DEPOSIT,
            payment_method=PaymentMethod.ETOPLATEZHI,
            external_id=str(operation_id) if operation_id else payment.order_id,
        )

        # Process referral topup
        try:
            from app.services.referral_service import process_referral_topup

            await process_referral_topup(db, user.id, payment.amount_kopeks, getattr(self, 'bot', None))
        except Exception as error:
            logger.error('Error processing referral topup (EtoPlatezhi)', error=error)

        if was_first_topup and not user.has_made_first_topup and not user.referred_by_id:
            user.has_made_first_topup = True
            await db.commit()

        await db.refresh(user)
        await db.refresh(payment)

        # Admin notification
        if getattr(self, 'bot', None):
            try:
                from app.services.admin_notification_service import AdminNotificationService

                notification_service = AdminNotificationService(self.bot)
                await notification_service.send_balance_topup_notification(
                    user,
                    transaction,
                    old_balance,
                    topup_status=topup_status,
                    referrer_info=referrer_info,
                    subscription=subscription,
                    promo_group=promo_group,
                    db=db,
                )
            except Exception as error:
                logger.error('Error sending admin notification (EtoPlatezhi)', error=error)

        # User notification
        if getattr(self, 'bot', None) and user.telegram_id:
            try:
                display_name = settings.get_etoplatezhi_display_name()

                keyboard = await self.build_topup_success_keyboard(user)
                message = (
                    '✅ <b>Пополнение успешно!</b>\n\n'
                    f'💰 Сумма: {settings.format_price(payment.amount_kopeks)}\n'
                    f'💳 Способ: {display_name}\n'
                    f'🆔 Транзакция: {transaction.id}\n\n'
                    'Баланс пополнен автоматически!'
                )

                await self.bot.send_message(
                    user.telegram_id,
                    message,
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
            except Exception as error:
                logger.error('Error sending user notification (EtoPlatezhi)', error=error)

        # Auto-purchase subscription + cart notification
        try:
            from app.services.payment.common import send_cart_notification_after_topup

            await send_cart_notification_after_topup(user, payment.amount_kopeks, db, getattr(self, 'bot', None))
        except Exception as error:
            logger.error(
                'Error with cart after EtoPlatezhi topup',
                user_id=user.id,
                error=error,
                exc_info=True,
            )

        logger.info(
            'EtoPlatezhi payment finalized',
            order_id=payment.order_id,
            user_id=payment.user_id,
            trigger=trigger,
        )

        return True

    async def check_etoplatezhi_payment_status(
        self,
        db: AsyncSession,
        order_id: str,
    ) -> dict[str, Any] | None:
        """Check payment status via Gate API.

        Args:
            db: DB session.
            order_id: Order ID.

        Returns:
            Status data from API.
        """
        try:
            status_data = await etoplatezhi_service.get_payment_status(order_id)
            return status_data
        except Exception as e:
            logger.exception('EtoPlatezhi: error checking status', e=e)
            return None

    async def get_etoplatezhi_payment_status(
        self,
        db: AsyncSession,
        local_payment_id: int,
    ) -> dict[str, Any] | None:
        """Check EtoPlatezhi payment status by local ID via Gate API.

        If the payment is confirmed — automatically credits balance.
        """
        logger.info('EtoPlatezhi: checking payment status', local_payment_id=local_payment_id)
        etoplatezhi_crud = import_module('app.database.crud.etoplatezhi')

        payment = await etoplatezhi_crud.get_etoplatezhi_payment_by_id(db, local_payment_id)
        if not payment:
            logger.warning('EtoPlatezhi payment not found', local_payment_id=local_payment_id)
            return None

        if payment.is_paid:
            return {
                'payment': payment,
                'status': 'success',
                'is_paid': True,
            }

        try:
            response = await etoplatezhi_service.get_payment_status(payment.order_id)

            # Parse status response
            resp_payment = response.get('payment', {})
            resp_status = str(resp_payment.get('status', ''))

            if resp_status == 'success':
                logger.info('EtoPlatezhi payment confirmed via API', order_id=payment.order_id)

                # Lock payment row
                locked = await etoplatezhi_crud.get_etoplatezhi_payment_by_id_for_update(db, payment.id)
                if not locked:
                    logger.error('EtoPlatezhi status check: failed to lock payment', payment_id=payment.id)
                elif locked.is_paid:
                    logger.info('EtoPlatezhi payment already paid after lock', order_id=locked.order_id)
                    payment = locked
                else:
                    payment = locked

                    resp_operations = response.get('operations', [])
                    operation_id = None
                    if resp_operations:
                        last_op = resp_operations[-1] if isinstance(resp_operations, list) else resp_operations
                        operation_id = str(last_op.get('id', ''))

                    callback_payload = {
                        'check_source': 'api',
                        'api_response': response,
                    }

                    payment.status = 'success'
                    payment.is_paid = True
                    payment.paid_at = datetime.now(UTC)
                    payment.callback_payload = callback_payload
                    payment.etoplatezhi_operation_id = operation_id
                    payment.updated_at = datetime.now(UTC)

                    # Extract token if present
                    resp_account = response.get('account', {})
                    if resp_account.get('token'):
                        payment.account_token = resp_account['token']

                    await db.flush()

                    await self._finalize_etoplatezhi_payment(
                        db,
                        payment,
                        operation_id=operation_id,
                        trigger='api_check',
                    )
        except Exception as e:
            logger.error('Error checking EtoPlatezhi payment status', e=e)

        return {
            'payment': payment,
            'status': payment.status or 'pending',
            'is_paid': payment.is_paid,
        }
