"""Plan prices (base: naira) + daily exchange rates so visitors can see prices in their own currency."""
import logging
import time

import requests

from config import CONTACT_URL, PLANS

log = logging.getLogger(__name__)
CURRENCIES = ["NGN", "USD", "GBP", "EUR", "GHS", "KES", "ZAR", "CAD", "AUD", "INR", "BRL", "AED", "EGP", "TZS",
              "UGX", "MAD", "XOF", "XAF", "MXN", "SAR", "TRY", "CHF"]
_cache = {"t": 0.0, "rates": {}, "as_of": None}


def _refresh():
    if _cache["rates"] and time.time() - _cache["t"] < 12 * 3600:
        return
    try:
        d = requests.get("https://open.er-api.com/v6/latest/NGN", timeout=8).json()
        if d.get("result") == "success":
            rates = d["rates"]
            _cache.update(t=time.time(), rates={c: rates[c] for c in CURRENCIES if c in rates},
                          as_of=d.get("time_last_update_utc"))
    except Exception as exc:  # keep serving stale rates, or NGN only
        log.warning("exchange-rate refresh failed: %s", exc)


def payload():
    _refresh()
    return {"base": "NGN", "plans": PLANS, "rates": _cache["rates"], "as_of": _cache["as_of"],
            "contact_url": CONTACT_URL}
