"""Flutterwave Standard (v3) checkout. Every payment is verified server-side with the secret key:
neither the browser redirect nor the webhook body is trusted on its own."""
import hmac

import requests

import track
from config import FLW_SECRET_HASH, FLW_SECRET_KEY, PUBLIC_URL

API = "https://api.flutterwave.com/v3"


class PayError(Exception):
    pass


def enabled() -> bool:
    """Payments only switch on when keys are set AND records will survive restarts (Postgres)."""
    return bool(FLW_SECRET_KEY and PUBLIC_URL and FLW_SECRET_HASH) and track.storage_kind() != "temporary"


def _headers():
    return {"Authorization": f"Bearer {FLW_SECRET_KEY}", "Content-Type": "application/json"}


def init_checkout(tx_ref, amount, currency, email, redirect_url, title, description) -> str:
    try:
        r = requests.post(f"{API}/payments", headers=_headers(), timeout=20, json={
            "tx_ref": tx_ref, "amount": amount, "currency": currency, "redirect_url": redirect_url,
            "customer": {"email": email, "name": email.split("@")[0][:40] or "Customer"},
            "customizations": {"title": title, "description": description},
        })
        body = r.json()
    except Exception as exc:
        raise PayError(f"Could not reach Flutterwave: {exc.__class__.__name__}")
    link = (body.get("data") or {}).get("link")
    if r.status_code != 200 or body.get("status") != "success" or not link:
        raise PayError("Flutterwave refused to start the payment")
    return link


def verify(transaction_id=None, tx_ref=None) -> dict:
    """Ask Flutterwave directly what happened to this payment."""
    try:
        if transaction_id:
            r = requests.get(f"{API}/transactions/{transaction_id}/verify", headers=_headers(), timeout=20)
        else:
            r = requests.get(f"{API}/transactions/verify_by_reference", params={"tx_ref": tx_ref}, headers=_headers(), timeout=20)
        body = r.json()
    except Exception as exc:
        raise PayError(f"Verification failed: {exc.__class__.__name__}")
    if r.status_code != 200 or body.get("status") != "success" or not isinstance(body.get("data"), dict):
        raise PayError("Verification not available")
    return body["data"]


def check_ok(data: dict, payment: dict) -> bool:
    """The payment really succeeded, for THIS reference, in the right currency, for at least the plan price."""
    try:
        return (str(data.get("status", "")).lower() == "successful"
                and data.get("tx_ref") == payment["tx_ref"]
                and data.get("currency") == payment["currency"]
                and float(data.get("amount", 0)) + 0.01 >= float(payment["amount"]))
    except (TypeError, ValueError):
        return False


def webhook_ok(header_value) -> bool:
    return bool(FLW_SECRET_HASH) and bool(header_value) and hmac.compare_digest(str(header_value), FLW_SECRET_HASH)
