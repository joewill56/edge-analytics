"""Track record: locks each pick before kickoff, settles it from real results, reports honest stats.

Storage: Postgres if DATABASE_URL is set (survives restarts, e.g. a free Neon database),
otherwise SQLite (fine locally; wiped on free Render restarts).
"""
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import db
import pipeline
from config import DATABASE_URL, DB_PATH

log = logging.getLogger(__name__)

WIN = {"1": {"H"}, "X": {"D"}, "2": {"A"}, "1X": {"H", "D"}, "X2": {"D", "A"}, "12": {"H", "A"}}
SCHEMA = """CREATE TABLE IF NOT EXISTS picks (
    event_id TEXT PRIMARY KEY, sport_key TEXT NOT NULL, league TEXT NOT NULL,
    home TEXT NOT NULL, away TEXT NOT NULL, home_fd TEXT NOT NULL, away_fd TEXT NOT NULL,
    commence TEXT NOT NULL, pick_code TEXT NOT NULL, pick_prob REAL NOT NULL, fair_odds REAL,
    market_prob REAL, p_home REAL, p_draw REAL, p_away REAL,
    odds_h REAL, odds_d REAL, odds_a REAL,
    created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
    result TEXT, score TEXT, settled_at TEXT)"""
INSERT_COLS = ["event_id", "sport_key", "league", "home", "away", "home_fd", "away_fd", "commence",
               "pick_code", "pick_prob", "fair_odds", "market_prob", "p_home", "p_draw", "p_away",
               "odds_h", "odds_d", "odds_a", "created_at"]

_ready = False
_settle_lock = threading.Lock()
_last_ingest: dict = {}


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(sql, params=(), fetch=False, many=None):
    """Run one statement (or many) on whichever database is configured."""
    if DATABASE_URL:
        import psycopg2
        import psycopg2.extras
        con = psycopg2.connect(DATABASE_URL, connect_timeout=10)
        try:
            with con, con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                sql = sql.replace("?", "%s")
                if many is not None:
                    for row in many:
                        cur.execute(sql, row)
                    return None
                cur.execute(sql, params)
                return [dict(r) for r in cur.fetchall()] if fetch else None
        finally:
            con.close()
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        with con:
            if many is not None:
                con.executemany(sql, many)
                return None
            cur = con.execute(sql, params)
            return [dict(r) for r in cur.fetchall()] if fetch else None
    finally:
        con.close()


def _ensure():
    global _ready
    if not _ready:
        _run(SCHEMA)
        _ready = True


def storage_kind():
    if DATABASE_URL:
        return "postgres"
    return "temporary" if os.getenv("RENDER") else "local"


def record_picks(entries):
    """entries: [(item, sport_key, league_code, home_fd, away_fd)]. Locks a pick the first time it is
    seen, only if kickoff is still in the future. Never overwrites an existing pick."""
    _ensure()
    rows = []
    for item, sport_key, code, h_fd, a_fd in entries:
        pk, m = item.get("pick"), item.get("model")
        if not pk or not m or pk["code"] == "SKIP":
            continue
        try:
            kickoff = datetime.fromisoformat(item["commence"].replace("Z", "+00:00"))
        except ValueError:
            continue
        if kickoff <= _now():
            continue  # never record after kickoff: no hindsight
        b = item["best"]
        bl = m["blend"]
        rows.append((item["id"], sport_key, code, item["home"], item["away"], h_fd, a_fd, item["commence"],
                     pk["code"], pk["prob"], pk["fair_odds"], pk.get("market_prob"), bl[0], bl[1], bl[2],
                     b[0]["odds"], b[1]["odds"], b[2]["odds"], _iso(_now())))
    if rows:
        sql = (f"INSERT INTO picks ({','.join(INSERT_COLS)}) VALUES ({','.join('?' * len(INSERT_COLS))}) "
               "ON CONFLICT (event_id) DO NOTHING")
        _run(sql, many=rows)


def settle():
    if not _settle_lock.acquire(blocking=False):
        return
    try:
        _ensure()
        cutoff = _iso(_now() - timedelta(hours=3))
        pend = _run("SELECT * FROM picks WHERE status='pending' AND commence < ?", (cutoff,), fetch=True) or []
        for code in {p["league"] for p in pend}:
            if time.time() - _last_ingest.get(code, 0) > 3600:  # free CSV refresh, at most hourly per league
                try:
                    pipeline.ingest_league(code, 1)
                except Exception as exc:
                    log.warning("result refresh failed for %s: %s", code, exc)
                _last_ingest[code] = time.time()
        for p in pend:
            ko = datetime.fromisoformat(p["commence"].replace("Z", "+00:00"))
            lo, hi = (ko - timedelta(days=1)).strftime("%Y-%m-%d"), (ko + timedelta(days=1)).strftime("%Y-%m-%d")
            with db.conn() as c:
                r = c.execute(
                    "SELECT hg, ag, result FROM matches WHERE league=? AND home=? AND away=? AND date BETWEEN ? AND ?",
                    (p["league"], p["home_fd"], p["away_fd"], lo, hi)).fetchone()
            if r:
                status = "won" if r[2] in WIN[p["pick_code"]] else "lost"
                _run("UPDATE picks SET status=?, result=?, score=?, settled_at=? WHERE event_id=?",
                     (status, r[2], f"{r[0]}-{r[1]}", _iso(_now()), p["event_id"]))
            elif _now() - ko > timedelta(days=10):  # postponed / never reported
                _run("UPDATE picks SET status='void', settled_at=? WHERE event_id=?", (_iso(_now()), p["event_id"]))
    except Exception:
        log.exception("settle failed")
    finally:
        _settle_lock.release()


def settle_async():
    threading.Thread(target=settle, daemon=True).start()


def summary():
    _ensure()
    rows = _run("SELECT * FROM picks ORDER BY commence DESC LIMIT 3000", fetch=True) or []
    done = [r for r in rows if r["status"] in ("won", "lost")]

    def block(rs):
        n = len(rs)
        wins = sum(r["status"] == "won" for r in rs)
        return {"settled": n, "wins": wins,
                "hit_rate": round(wins / n, 4) if n else None,
                "avg_predicted": round(sum(r["pick_prob"] for r in rs) / n, 4) if n else None}

    by_type = {}
    for code in ("1", "2", "X", "1X", "X2", "12"):
        rs = [r for r in done if r["pick_code"] == code]
        if rs:
            by_type[code] = block(rs)

    singles = [r for r in done if r["pick_code"] in ("1", "X", "2")]
    roi = None
    if singles:
        key = {"1": "odds_h", "X": "odds_d", "2": "odds_a"}
        profit = sum((r[key[r["pick_code"]]] - 1) if r["status"] == "won" else -1 for r in singles)
        roi = {"bets": len(singles), "roi": round(profit / len(singles), 4)}

    recent = [{k: r[k] for k in ("home", "away", "commence", "pick_code", "pick_prob", "status", "score")}
              for r in rows[:40]]
    return {"overall": block(done), "by_type": by_type, "singles_roi": roi, "recent": recent,
            "pending": sum(r["status"] == "pending" for r in rows), "storage": storage_kind()}
