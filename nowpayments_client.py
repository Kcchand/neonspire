# nowpayments_client.py
import os
import requests


class NowPaymentsError(Exception):
    """Simple wrapper for NOWPayments errors."""
    pass


def _get_cfg():
    api_key = os.getenv("NOWPAYMENTS_API_KEY")
    base_url = os.getenv("NOWPAYMENTS_BASE_URL", "https://api.nowpayments.io/v1")
    ipn_url = os.getenv("NOWPAYMENTS_IPN_URL")
    success_url = os.getenv("NOWPAYMENTS_SUCCESS_URL")
    cancel_url = os.getenv("NOWPAYMENTS_CANCEL_URL")
    pay_currency = os.getenv("NOWPAYMENTS_PAY_CURRENCY", "usdttrc20")
    price_currency = os.getenv("NOWPAYMENTS_PRICE_CURRENCY", "usd")

    if not api_key:
        raise NowPaymentsError("NOWPAYMENTS_API_KEY is missing in .env")

    return {
        "api_key": api_key,
        "base_url": base_url.rstrip("/"),
        "ipn_url": ipn_url,
        "success_url": success_url,
        "cancel_url": cancel_url,
        "pay_currency": pay_currency,
        "price_currency": price_currency,
    }


def create_invoice(
    amount: float,
    order_id: str,
    description: str,
) -> dict:
    """
    Create a NOWPayments invoice in USD, paid in USDT TRC20 (or whatever you set).

    This signature is what player_bp.py expects:
        create_invoice(amount=..., order_id=..., description=...)
    """
    cfg = _get_cfg()

    payload = {
        "price_amount": float(amount),
        "price_currency": cfg["price_currency"],   # e.g. "usd"
        "pay_currency": cfg["pay_currency"],       # e.g. "usdttrc20"
        "order_id": order_id,
        "order_description": description,
        "ipn_callback_url": cfg["ipn_url"],
        "success_url": cfg["success_url"],
        "cancel_url": cfg["cancel_url"],
    }

    headers = {
        "x-api-key": cfg["api_key"],
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    resp = requests.post(
        cfg["base_url"] + "/invoice",
        json=payload,
        headers=headers,
        timeout=15,
    )

    if not resp.ok:
        raise NowPaymentsError(f"HTTP {resp.status_code}: {resp.text}")

    data = resp.json()
    # NOWPayments usually returns keys: id, order_id, invoice_url, pay_currency, price_amount, etc.
    if "invoice_url" not in data:
        raise NowPaymentsError(f"Unexpected response from NOWPayments: {data}")

    return data