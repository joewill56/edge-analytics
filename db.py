import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import pandas as pd

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    league TEXT NOT NULL, season TEXT, date TEXT NOT NULL,
    home TEXT NOT NULL, away TEXT NOT NULL,
    hg INTEGER NOT NULL, ag INTEGER NOT NULL, result TEXT NOT NULL,
    b365h REAL, b365d REAL, b365a REAL, b365o25 REAL, b365u25 REAL,
    PRIMARY KEY (league, date, home, away)
);
CREATE TABLE IF NOT EXISTS odds_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at TEXT NOT NULL, sport_key TEXT NOT NULL, payload TEXT NOT NULL
);
"""


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    with conn() as c:
        c.executescript(SCHEMA)
        have = {r[1] for r in c.execute("PRAGMA table_info(matches)")}
        for col in ("b365o25", "b365u25"):  # migrate databases created before over/under odds were stored
            if col not in have:
                c.execute(f"ALTER TABLE matches ADD COLUMN {col} REAL")


def upsert_matches(df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    cols = ["league", "season", "date", "home", "away", "hg", "ag", "result", "b365h", "b365d", "b365a", "b365o25", "b365u25"]
    rows = [tuple(None if pd.isna(v) else v for v in r) for r in df[cols].itertuples(index=False)]
    with conn() as c:
        c.executemany(
            f"INSERT OR REPLACE INTO matches ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", rows
        )
    return len(rows)


def load_matches(league: str) -> pd.DataFrame:
    init_db()
    with conn() as c:
        df = pd.read_sql_query("SELECT * FROM matches WHERE league=? ORDER BY date", c, params=(league,))
    df["date"] = pd.to_datetime(df["date"])
    return df


def save_snapshot(sport_key: str, events: list) -> None:
    init_db()
    with conn() as c:
        c.execute(
            "INSERT INTO odds_snapshots (fetched_at, sport_key, payload) VALUES (?,?,?)",
            (datetime.now(timezone.utc).isoformat(), sport_key, json.dumps(events)),
        )
        # keep the table small
        c.execute("DELETE FROM odds_snapshots WHERE id NOT IN (SELECT id FROM odds_snapshots ORDER BY id DESC LIMIT 200)")
