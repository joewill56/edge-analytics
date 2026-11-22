"""Chronological feature engineering: Elo ratings + rolling form.

Features for a match are computed BEFORE its result updates the state,
so there is no look-ahead leakage.
"""
import math
from collections import deque

import numpy as np
import pandas as pd

K = 20.0
HOME_ADV = 65.0
BASE = 1500.0
FORM_N = 5
MIN_GAMES = 3
RESULT_MAP = {"H": 0, "D": 1, "A": 2}
FEATURES = ["elo_diff", "home_elo", "away_elo", "h_pts", "h_gf", "h_ga", "a_pts", "a_gf", "a_ga"]


class State:
    def __init__(self):
        self.elo: dict[str, float] = {}
        self.hist: dict[str, deque] = {}
        self.games: dict[str, int] = {}

    def _elo(self, t):
        return self.elo.get(t, BASE)

    def _form(self, t):
        h = self.hist.get(t)
        if not h:
            return 1.4, 1.4, 1.4  # neutral priors: pts, goals for, goals against
        arr = np.array(h, dtype=float)
        return tuple(arr.mean(axis=0))

    def features(self, home, away):
        he, ae = self._elo(home), self._elo(away)
        hp, hgf, hga = self._form(home)
        ap, agf, aga = self._form(away)
        return {
            "elo_diff": he + HOME_ADV - ae, "home_elo": he, "away_elo": ae,
            "h_pts": hp, "h_gf": hgf, "h_ga": hga, "a_pts": ap, "a_gf": agf, "a_ga": aga,
        }

    def update(self, home, away, hg, ag):
        he, ae = self._elo(home), self._elo(away)
        exp_home = 1 / (1 + 10 ** (-(he + HOME_ADV - ae) / 400))
        score = 1.0 if hg > ag else 0.5 if hg == ag else 0.0
        mult = math.log(abs(hg - ag) + 1) + 1
        delta = K * mult * (score - exp_home)
        self.elo[home], self.elo[away] = he + delta, ae - delta
        hpts = 3 if hg > ag else 1 if hg == ag else 0
        apts = 3 if ag > hg else 1 if hg == ag else 0
        self.hist.setdefault(home, deque(maxlen=FORM_N)).append((hpts, hg, ag))
        self.hist.setdefault(away, deque(maxlen=FORM_N)).append((apts, ag, hg))
        self.games[home] = self.games.get(home, 0) + 1
        self.games[away] = self.games.get(away, 0) + 1


def build_training_frame(df: pd.DataFrame):
    """Returns (feature frame with labels + market odds, final State)."""
    state = State()
    rows = []
    for r in df.sort_values("date").itertuples(index=False):
        if state.games.get(r.home, 0) >= MIN_GAMES and state.games.get(r.away, 0) >= MIN_GAMES:
            rows.append({
                **state.features(r.home, r.away),
                "y": RESULT_MAP[r.result], "date": r.date,
                "b365h": r.b365h, "b365d": r.b365d, "b365a": r.b365a,
                "hg": int(r.hg), "ag": int(r.ag), "bo": r.b365o25, "bu": r.b365u25,
            })
        state.update(r.home, r.away, int(r.hg), int(r.ag))
    return pd.DataFrame(rows), state


def implied_probs(h, d, a):
    """Remove the bookmaker margin by proportional normalisation."""
    inv = np.array([1 / h, 1 / d, 1 / a], dtype=float)
    return inv / inv.sum()
