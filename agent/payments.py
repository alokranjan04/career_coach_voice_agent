"""Razorpay: day-pass orders, checkout signature check and webhook check.
Checkout offers UPI, cards and netbanking. All blocking."""
import hashlib
import hmac

import requests

from agent import config

API = "https://api.razorpay.com/v1"


class PaymentError(Exception):
    pass


def enabled() -> bool:
    return bool(config.RAZORPAY_KEY_ID and config.RAZORPAY_KEY_SECRET)


def create_order(phone: str) -> dict:
    r = requests.post(f"{API}/orders", auth=(config.RAZORPAY_KEY_ID, config.RAZORPAY_KEY_SECRET),
                      timeout=15, json={"amount": config.DAY_PASS_PAISE, "currency": "INR",
                                        "receipt": f"pass-{phone[-4:]}", "notes": {"phone": phone,
                                                                                    "product": "day_pass"}})
    if r.status_code >= 300:
        raise PaymentError(f"order failed {r.status_code}: {r.text[:200]}")
    return r.json()


def checkout_signature_ok(order_id: str, payment_id: str, signature: str) -> bool:
    expected = hmac.new(config.RAZORPAY_KEY_SECRET.encode(), f"{order_id}|{payment_id}".encode(),
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def webhook_signature_ok(raw_body: bytes, signature: str) -> bool:
    if not config.RAZORPAY_WEBHOOK_SECRET:
        return False
    expected = hmac.new(config.RAZORPAY_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def fetch_payment(payment_id: str) -> dict:
    """Confirm with Razorpay that the payment is really captured or authorised."""
    r = requests.get(f"{API}/payments/{payment_id}", timeout=15,
                     auth=(config.RAZORPAY_KEY_ID, config.RAZORPAY_KEY_SECRET))
    r.raise_for_status()
    return r.json()
