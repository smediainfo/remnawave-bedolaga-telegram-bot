"""API-client for EtoPlatezhi payment platform (paymentpage.etoplatezhi.ru).

Payment Page flow: redirect user to a signed URL, receive callback on completion.
Gate API: status checks, refunds, recurrent charges.

Docs: https://developers.etoplatezhi.ru/landing-ru/
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from typing import Any
from urllib.parse import urlencode

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

# Payment Page URL (redirect model)
DEFAULT_PAYMENT_PAGE_URL = 'https://paymentpage.etoplatezhi.ru/payment'

# Gate API base URL
DEFAULT_GATE_URL = 'https://api.etoplatezhi.ru'

# Payment method force codes
ETOPLATEZHI_METHODS = {
    'card': None,                    # default — cards (Visa/MC/MIR)
    'sberpay': 'sberpay',            # SberPay
    'tpay': 'tinkoff-pay',           # T-Pay (Tinkoff)
    'sbp': 'sbp',                    # SBP (Система быстрых платежей)
}

# ── Natural sort helper ────────────────────────────────────────────────

_NATURAL_RE = re.compile(r'(\d+)')


def _natural_sort_key(s: str) -> list[str | int]:
    """Natural (human) sort key: 'a2' < 'a10'."""
    parts: list[str | int] = []
    for token in _NATURAL_RE.split(s):
        if token.isdigit():
            parts.append(int(token))
        else:
            parts.append(token.lower())
    return parts


# ── Signature ──────────────────────────────────────────────────────────


def _flatten_params(data: dict[str, Any], prefix: str = '') -> list[str]:
    """Recursively flatten a dict into 'parent:child:value' strings.

    Rules (per EtoPlatezhi docs):
    * booleans → '0' / '1'
    * None / empty string → 'key:'
    * empty list → skip
    * list items → indexed 0, 1, …
    """
    parts: list[str] = []
    for key, value in data.items():
        if key == 'signature':
            continue
        path = f'{prefix}:{key}' if prefix else str(key)

        if isinstance(value, dict):
            parts.extend(_flatten_params(value, path))
        elif isinstance(value, list):
            if not value:
                continue
            for idx, item in enumerate(value):
                indexed_path = f'{path}:{idx}'
                if isinstance(item, dict):
                    parts.extend(_flatten_params(item, indexed_path))
                else:
                    parts.append(f'{indexed_path}:{_scalar_to_str(item)}')
        else:
            parts.append(f'{path}:{_scalar_to_str(value)}')
    return parts


def _scalar_to_str(value: Any) -> str:
    if value is True:
        return '1'
    if value is False:
        return '0'
    if value is None:
        return ''
    return str(value)


def generate_signature(params: dict[str, Any], secret_key: str) -> str:
    """Generate HMAC-SHA-512 signature for EtoPlatezhi.

    Algorithm:
    1. Flatten params to 'path:value' strings (skip 'signature')
    2. Sort strings in natural order
    3. Join with ';'
    4. HMAC-SHA-512 with secret_key
    5. Base64-encode the result
    """
    flat = _flatten_params(params)
    flat.sort(key=_natural_sort_key)
    message = ';'.join(flat)

    mac = hmac.new(
        secret_key.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha512,
    )
    return base64.b64encode(mac.digest()).decode('utf-8')


def verify_signature(params: dict[str, Any], secret_key: str) -> bool:
    """Verify callback signature from EtoPlatezhi."""
    received_sig = params.get('signature')
    if not received_sig:
        return False
    expected = generate_signature(params, secret_key)
    return hmac.compare_digest(expected, received_sig)


# ── Service class ──────────────────────────────────────────────────────


class EtoplatezhiService:
    """API client for EtoPlatezhi platform."""

    def __init__(self) -> None:
        self._project_id: int | None = None
        self._secret_key: str | None = None
        self._payment_page_url: str | None = None
        self._gate_url: str | None = None
        self._callback_url: str | None = None
        self._success_url: str | None = None
        self._fail_url: str | None = None

    # ── Config properties ──────────────────────────────────────────

    @property
    def project_id(self) -> int:
        if self._project_id is None:
            self._project_id = settings.ETOPLATEZHI_PROJECT_ID
        return self._project_id or 0

    @property
    def secret_key(self) -> str:
        if self._secret_key is None:
            self._secret_key = settings.ETOPLATEZHI_SECRET_KEY
        return self._secret_key or ''

    @property
    def payment_page_url(self) -> str:
        if self._payment_page_url is None:
            self._payment_page_url = getattr(
                settings, 'ETOPLATEZHI_PAYMENT_PAGE_URL', None,
            ) or DEFAULT_PAYMENT_PAGE_URL
        return self._payment_page_url

    @property
    def gate_url(self) -> str:
        if self._gate_url is None:
            self._gate_url = getattr(
                settings, 'ETOPLATEZHI_GATE_URL', None,
            ) or DEFAULT_GATE_URL
        return self._gate_url

    @property
    def callback_url(self) -> str | None:
        if self._callback_url is None:
            self._callback_url = getattr(settings, 'ETOPLATEZHI_CALLBACK_URL', None)
        return self._callback_url

    @property
    def success_url(self) -> str | None:
        if self._success_url is None:
            self._success_url = getattr(settings, 'ETOPLATEZHI_SUCCESS_URL', None)
        return self._success_url

    @property
    def fail_url(self) -> str | None:
        if self._fail_url is None:
            self._fail_url = getattr(settings, 'ETOPLATEZHI_FAIL_URL', None)
        return self._fail_url

    # ── Payment Page (redirect) ────────────────────────────────────

    def build_payment_url(
        self,
        *,
        payment_id: str,
        amount: int,
        currency: str = 'RUB',
        customer_id: str,
        description: str | None = None,
        force_payment_method: str | None = None,
        language_code: str = 'ru',
        recurring_register: bool = False,
        recurring_type: str | None = None,
        recurring_period: str | None = None,
        recurring_interval: int | None = None,
        recurring_amount: int | None = None,
        recurring_start_date: str | None = None,
        recurring_scheduled_payment_id: str | None = None,
    ) -> str:
        """Build a signed redirect URL for Payment Page.

        Args:
            payment_id: Unique payment identifier (max 255 chars).
            amount: Amount in minor currency units (kopeks for RUB).
            currency: ISO 4217 code.
            customer_id: Unique customer identifier in the project.
            description: Short payment description (max 255 chars).
            force_payment_method: Pre-select method ('sberpay', 'tinkoff-pay', 'sbp').
            language_code: Form language ('ru', 'en').
            recurring_register: Register recurring on this payment.
            recurring_type: 'R' for regular (merchant-initiated), 'C' for express, 'U' for auto.
            recurring_period: 'M' month, 'W' week, 'D' day, 'Y' year.
            recurring_interval: Interval count (1 = every period).
            recurring_amount: Fixed charge amount for recurring.
            recurring_start_date: First recurring charge date 'dd-mm-yyyy'.
            recurring_scheduled_payment_id: Unique ID for recurring series.

        Returns:
            Fully signed URL to redirect the customer to.
        """
        params: dict[str, Any] = {
            'project_id': self.project_id,
            'payment_id': payment_id,
            'payment_amount': amount,
            'payment_currency': currency,
            'customer_id': customer_id,
        }

        if description:
            params['payment_description'] = description

        if force_payment_method:
            params['force_payment_method'] = force_payment_method

        if language_code:
            params['language_code'] = language_code

        if self.callback_url:
            params['merchant_callback_url'] = self.callback_url

        if self.success_url:
            params['merchant_success_url'] = self.success_url
            params['merchant_success_enabled'] = 1

        if self.fail_url:
            params['merchant_fail_url'] = self.fail_url
            params['merchant_fail_enabled'] = 1

        # Recurring params
        if recurring_register:
            recurring: dict[str, Any] = {'register': True}
            if recurring_type:
                recurring['type'] = recurring_type
            if recurring_period:
                recurring['period'] = recurring_period
            if recurring_interval is not None:
                recurring['interval'] = recurring_interval
            if recurring_amount is not None:
                recurring['amount'] = recurring_amount
            if recurring_start_date:
                recurring['start_date'] = recurring_start_date
            if recurring_scheduled_payment_id:
                recurring['scheduled_payment_id'] = recurring_scheduled_payment_id
            params['recurring'] = recurring

        # Sign
        params['signature'] = generate_signature(params, self.secret_key)

        # For URL, we need to flatten recurring object into base64 or JSON string
        url_params = self._prepare_url_params(params)

        return f'{self.payment_page_url}?{urlencode(url_params)}'

    @staticmethod
    def _prepare_url_params(params: dict[str, Any]) -> dict[str, str]:
        """Prepare params for URL query string.

        Nested dicts (like 'recurring') are base64-encoded as required by Payment Page.
        """
        import json as json_mod

        url_params: dict[str, str] = {}
        for key, value in params.items():
            if isinstance(value, dict):
                # Payment Page expects nested objects as base64-encoded JSON
                json_str = json_mod.dumps(value, separators=(',', ':'))
                url_params[key] = base64.b64encode(json_str.encode('utf-8')).decode('utf-8')
            elif isinstance(value, bool):
                url_params[key] = '1' if value else '0'
            else:
                url_params[key] = str(value)
        return url_params

    # ── Callback verification ──────────────────────────────────────

    def verify_callback(self, data: dict[str, Any]) -> bool:
        """Verify incoming callback signature."""
        return verify_signature(data, self.secret_key)

    # ── Gate API helpers ───────────────────────────────────────────

    async def _gate_request(
        self,
        endpoint: str,
        params: dict[str, Any],
        *,
        timeout: int = 30,
    ) -> dict[str, Any]:
        """Send a signed request to Gate API.

        Args:
            endpoint: API path (e.g. '/v2/payment/status').
            params: Request body (signature will be added).
            timeout: Request timeout in seconds.

        Returns:
            Parsed JSON response.
        """
        params['signature'] = generate_signature(params, self.secret_key)
        url = f'{self.gate_url}{endpoint}'

        logger.info(
            'EtoPlatezhi Gate request',
            endpoint=endpoint,
            project_id=self.project_id,
        )

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(
                    url,
                    json=params,
                    headers={'Content-Type': 'application/json'},
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as response,
            ):
                text = await response.text()
                logger.debug('EtoPlatezhi Gate response', status=response.status, text=text[:500])

                if response.status != 200:
                    logger.error(
                        'EtoPlatezhi Gate HTTP error',
                        status=response.status,
                        text=text[:500],
                    )
                    raise Exception(f'EtoPlatezhi Gate HTTP {response.status}')

                return await response.json()
        except aiohttp.ClientError as e:
            logger.exception('EtoPlatezhi Gate connection error', error=e)
            raise

    async def get_payment_status(self, payment_id: str) -> dict[str, Any]:
        """Check payment status via Gate API.

        POST /v2/payment/status
        """
        params = {
            'project_id': self.project_id,
            'payment_id': payment_id,
        }
        return await self._gate_request('/v2/payment/status', params)

    async def create_refund(
        self,
        payment_id: str,
        *,
        amount: int | None = None,
        currency: str = 'RUB',
    ) -> dict[str, Any]:
        """Create a refund via Gate API.

        POST /v2/payment/refund

        Args:
            payment_id: Original payment ID.
            amount: Refund amount in minor units (partial refund). None = full refund.
            currency: ISO 4217 code.

        Returns:
            Refund operation response.
        """
        params: dict[str, Any] = {
            'project_id': self.project_id,
            'payment_id': payment_id,
        }
        if amount is not None:
            params['payment_amount'] = amount
            params['payment_currency'] = currency

        logger.info(
            'EtoPlatezhi: creating refund',
            payment_id=payment_id,
            amount=amount,
        )

        return await self._gate_request('/v2/payment/refund', params)

    async def charge_recurrent(
        self,
        *,
        payment_id: str,
        token: str,
        amount: int,
        currency: str = 'RUB',
        customer_id: str | None = None,
        scheduled_payment_id: str | None = None,
    ) -> dict[str, Any]:
        """Initiate a recurring charge via Gate API.

        POST /v2/payment/recurring

        Args:
            payment_id: New unique payment ID for this charge.
            token: Account token from initial registration callback.
            amount: Amount in minor currency units.
            currency: ISO 4217 code.
            customer_id: Customer identifier.
            scheduled_payment_id: Recurring series identifier.

        Returns:
            Charge result.
        """
        params: dict[str, Any] = {
            'project_id': self.project_id,
            'payment_id': payment_id,
            'payment_amount': amount,
            'payment_currency': currency,
            'account_token': token,
        }
        if customer_id:
            params['customer_id'] = customer_id
        if scheduled_payment_id:
            params['recurring'] = {
                'scheduled_payment_id': scheduled_payment_id,
            }

        logger.info(
            'EtoPlatezhi: recurring charge',
            payment_id=payment_id,
            amount=amount,
            token=token[:8] + '***' if token else None,
        )

        return await self._gate_request('/v2/payment/recurring', params)

    async def cancel_recurring(
        self,
        *,
        recurring_id: str,
    ) -> dict[str, Any]:
        """Cancel a recurring subscription via Gate API.

        POST /v2/recurring/cancel

        Args:
            recurring_id: Recurring series ID (from registration callback).

        Returns:
            Cancellation result.
        """
        params: dict[str, Any] = {
            'project_id': self.project_id,
            'recurring_id': recurring_id,
        }

        logger.info('EtoPlatezhi: cancelling recurring', recurring_id=recurring_id)

        return await self._gate_request('/v2/recurring/cancel', params)


# Singleton instance
etoplatezhi_service = EtoplatezhiService()
