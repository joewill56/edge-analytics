"""Live odds from The Odds API (free tier: 500 credits/month). Cached to save quota."""
import difflib
import re
import time
from datetime import datetime, timezone

import requests

import db
from config import ODDS_API_KEY, ODDS_CACHE_TTL, ODDS_REGIONS

URL = "https://api.the-odds-api.com/v4/sports/{sport}/odds/"
_cache: dict = {}


class OddsError(Exception):
    pass


ALIASES = {
    "manchester united": "Man United", "manchester city": "Man City", "tottenham hotspur": "Tottenham",
    "newcastle united": "Newcastle", "nottingham forest": "Nott'm Forest", "wolverhampton wanderers": "Wolves",
    "brighton and hove albion": "Brighton", "west ham united": "West Ham", "leeds united": "Leeds",
    "leicester city": "Leicester", "luton town": "Luton", "atletico madrid": "Ath Madrid",
    "athletic bilbao": "Ath Bilbao", "real betis": "Betis", "real sociedad": "Sociedad",
    "borussia dortmund": "Dortmund", "bayer leverkusen": "Leverkusen",
    "borussia monchengladbach": "M'gladbach", "paris saint germain": "Paris SG", "inter milan": "Inter",
    "ac milan": "Milan", "as roma": "Roma", "olympique marseille": "Marseille", "olympique lyonnais": "Lyon",
    "eintracht frankfurt": "Ein Frankfurt", "fc koln": "FC Koln", "1 fc heidenheim": "Heidenheim",
    "rayo vallecano": "Vallecano", "celta vigo": "Celta", "espanyol": "Espanol", "rc strasbourg": "Strasbourg",
}


def _norm(s: str) -> str:
    s = s.lower().replace("é", "e").replace("ü", "u").replace("ö", "o").replace("í", "i").replace("ó", "o")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\b(fc|afc|cf|sc|ac|as|ss|us|1)\b", " ", s) if s.strip() not in ALIASES else s
    return re.sub(r"\s+", " ", s).strip()


def resolve_team(name: str, known: list[str]):
    """Map an Odds-API team name to the football-data name used in training."""
    if not known:
        return None
    if name in known:
        return name
    key = name.lower().replace("é", "e").replace("ü", "u").replace("ö", "o")
    key = re.sub(r"[^a-z0-9 ]", " ", key)
    key = re.sub(r"\s+", " ", key).strip()
    if key in ALIASES and ALIASES[key] in known:
        return ALIASES[key]
    norm_known = {_norm(k): k for k in known}
    n = _norm(name)
    if n in norm_known:
        return norm_known[n]
    hit = difflib.get_close_matches(n, list(norm_known), n=1, cutoff=0.6)
    return norm_known[hit[0]] if hit else None


def _parse_event(e: dict):
    home, away = e["home_team"], e["away_team"]
    books = []
    for b in e.get("bookmakers", []):
        m = next((m for m in b.get("markets", []) if m["key"] == "h2h"), None)
        if not m:
            continue
        px = {o["name"]: o["price"] for o in m["outcomes"]}
        if home in px and away in px and "Draw" in px:
            books.append({"book": b["title"], "odds": [px[home], px["Draw"], px[away]]})
    if not books:
        return None
    return {"id": e["id"], "home": home, "away": away, "commence": e["commence_time"], "books": books}


def cached_events(sport_key: str):
    """Events already in the cache (still fresh), or None. Never calls the API, so it never spends credits."""
    hit = _cache.get(sport_key)
    if hit and time.time() - hit["t"] < ODDS_CACHE_TTL:
        return hit["events"]
    return None


def get_odds(sport_key: str, force: bool = False):
    if not ODDS_API_KEY:
        raise OddsError("ODDS_API_KEY is not set. Get a free key at the-odds-api.com and add it to .env")
    hit = _cache.get(sport_key)
    if hit and not force and time.time() - hit["t"] < ODDS_CACHE_TTL:
        return hit["events"], hit["meta"]
    r = requests.get(
        URL.format(sport=sport_key),
        params={"apiKey": ODDS_API_KEY, "regions": ODDS_REGIONS, "markets": "h2h", "oddsFormat": "decimal"},
        timeout=20,
    )
    if r.status_code == 401:
        raise OddsError("Odds API rejected the key (401).")
    if r.status_code == 429:
        raise OddsError("Odds API quota exhausted (429).")
    r.raise_for_status()
    events = [ev for ev in (_parse_event(e) for e in r.json()) if ev]
    meta = {
        "remaining": r.headers.get("x-requests-remaining"),
        "used": r.headers.get("x-requests-used"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache[sport_key] = {"t": time.time(), "events": events, "meta": meta}
    try:
        db.save_snapshot(sport_key, events)
    except Exception:
        pass
    return events, meta
