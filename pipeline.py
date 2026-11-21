"""Historical data ingestion from football-data.co.uk (free CSVs, no key)."""
import io
import logging
from datetime import date

import pandas as pd
import requests

import db
from config import HIST_SEASONS, LEAGUES

log = logging.getLogger(__name__)
URL = "https://www.football-data.co.uk/mmz4281/{season}/{code}.csv"
REQUIRED = ["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR"]


def season_codes(n: int) -> list[str]:
    today = date.today()
    start = today.year if today.month >= 7 else today.year - 1
    return [f"{s % 100:02d}{(s + 1) % 100:02d}" for s in range(start - n + 1, start + 1)]


def fetch_season(code: str, season: str) -> pd.DataFrame:
    r = requests.get(URL.format(season=season, code=code), timeout=30)
    r.raise_for_status()
    raw = pd.read_csv(io.StringIO(r.content.decode("utf-8-sig", errors="replace")), on_bad_lines="skip")
    if not all(c in raw.columns for c in REQUIRED):
        return pd.DataFrame()
    raw = raw.dropna(subset=REQUIRED)
    out = pd.DataFrame({
        "league": code, "season": season,
        "date": pd.to_datetime(raw["Date"], dayfirst=True, errors="coerce"),
        "home": raw["HomeTeam"].str.strip(), "away": raw["AwayTeam"].str.strip(),
        "hg": pd.to_numeric(raw["FTHG"], errors="coerce"),
        "ag": pd.to_numeric(raw["FTAG"], errors="coerce"),
        "result": raw["FTR"].str.strip(),
    })
    for tgt, cands in {"b365h": ["B365H", "AvgH"], "b365d": ["B365D", "AvgD"], "b365a": ["B365A", "AvgA"]}.items():
        col = next((c for c in cands if c in raw.columns), None)
        out[tgt] = pd.to_numeric(raw[col], errors="coerce") if col else float("nan")
    out = out.dropna(subset=["date", "hg", "ag"])
    out = out[out["result"].isin(["H", "D", "A"])]
    out["hg"], out["ag"] = out["hg"].astype(int), out["ag"].astype(int)
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    return out


def ingest_league(code: str, n_seasons: int = HIST_SEASONS) -> int:
    db.init_db()
    total = 0
    for season in season_codes(n_seasons):
        try:
            total += db.upsert_matches(fetch_season(code, season))
        except Exception as exc:  # one bad season shouldn't kill the run
            log.warning("Skipping %s %s: %s", code, season, exc)
    return total


def ingest_all() -> dict:
    return {meta["code"]: ingest_league(meta["code"]) for meta in LEAGUES.values()}
