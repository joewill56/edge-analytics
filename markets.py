"""One scoreline distribution -> every goal market.

Pipeline per fixture:
  1. Poisson goal models give expected goals (home, away) + Dixon-Coles low-score correction.
  2. The home-vs-away split is anchored to the blended 1X2 probabilities (model + bookmakers).
  3. The full score matrix is built; GG, Over/Under, handicaps, combos, correct score all come from it,
     so every market is consistent with every other one.
"""
import numpy as np
from scipy.stats import poisson

from config import MARKET_MAX, MARKET_MIN

MAXG = 10
_K = np.arange(MAXG + 1)
I, J = np.meshgrid(_K, _K, indexing="ij")  # I = home goals (rows), J = away goals (cols)
DIFF, TOT = I - J, I + J


def score_matrix(lh, la, rho):
    m = np.outer(poisson.pmf(_K, lh), poisson.pmf(_K, la))
    m[0, 0] *= max(1 - lh * la * rho, 1e-6)
    m[0, 1] *= max(1 + lh * rho, 1e-6)
    m[1, 0] *= max(1 + la * rho, 1e-6)
    m[1, 1] *= max(1 - rho, 1e-6)
    return m / m.sum()


def anchor(lh, la, rho, target):
    """Keep the model's total goals, but split them so home-minus-away win probability matches `target`."""
    if target is None:
        return lh, la
    total, want = lh + la, target[0] - target[2]
    lo, hi = 0.02, 0.98
    for _ in range(25):
        s = (lo + hi) / 2
        m = score_matrix(total * s, total * (1 - s), rho)
        if m[DIFF > 0].sum() - m[DIFF < 0].sum() < want:
            lo = s
        else:
            hi = s
    s = (lo + hi) / 2
    return total * s, total * (1 - s)


def _fmt(x):
    return f"{x:+g}"


def binary_defs():
    """(group, key, label, boolean mask over scorelines) for every market with no push."""
    gg = (I > 0) & (J > 0)
    d = [("goals", "gg", "GG (both teams score)", gg), ("goals", "ng", "NG (a team keeps clean sheet)", ~gg)]
    for k in (1.5, 2.5, 3.5, 4.5, 5.5):
        d.append(("goals", f"over_{k}", f"Over {k}", TOT > k))
        d.append(("goals", f"under_{k}", f"Under {k}", TOT < k))
    d += [("double", "1x", "1X (Home or Draw)", DIFF >= 0), ("double", "x2", "X2 (Away or Draw)", DIFF <= 0),
          ("double", "12", "12 (no draw)", DIFF != 0)]
    for line in (-2.5, -1.5, 1.5, 2.5):
        d.append(("handicap", f"home_ah_{line}", f"Home {_fmt(line)} handicap", DIFF + line > 0))
        d.append(("handicap", f"away_ah_{line}", f"Away {_fmt(line)} handicap", -DIFF + line > 0))
    d += [("team", "home_o0.5", "Home team Over 0.5", I > 0), ("team", "home_o1.5", "Home team Over 1.5", I > 1),
          ("team", "away_o0.5", "Away team Over 0.5", J > 0), ("team", "away_o1.5", "Away team Over 1.5", J > 1),
          ("combo", "1x_o1.5", "1X & Over 1.5", (DIFF >= 0) & (TOT > 1)),
          ("combo", "x2_o1.5", "X2 & Over 1.5", (DIFF <= 0) & (TOT > 1)),
          ("combo", "1x_u3.5", "1X & Under 3.5", (DIFF >= 0) & (TOT < 4)),
          ("combo", "x2_u3.5", "X2 & Under 3.5", (DIFF <= 0) & (TOT < 4)),
          ("combo", "gg_o2.5", "GG & Over 2.5", gg & (TOT > 2))]
    return d


def outcome(mask, hg, ag):
    return bool(mask[min(int(hg), MAXG), min(int(ag), MAXG)])


def _push_market(m, side, line, label):
    """Asian handicap on a whole-number line: win / push (stake returned) / lose."""
    sign = 1 if side == "home" else -1
    v = sign * DIFF + line
    win, push, lose = float(m[v > 0].sum()), float(m[v == 0].sum()), float(m[v < 0].sum())
    return {"key": f"{side}_ah_{line}", "label": label, "prob": round(win, 4), "push": round(push, 4),
            "odds": round(1 + lose / win, 2) if win > 0.01 else None}


def build(lh_model, la_model, rho, target):
    lh, la = anchor(lh_model, la_model, rho, target)
    m = score_matrix(lh, la, rho)
    groups = {"goals": [], "double": [], "handicap": [], "team": [], "combo": []}
    for group, key, label, mask in binary_defs():
        p = float(m[mask].sum())
        groups[group].append({"key": key, "label": label, "prob": round(p, 4),
                              "odds": round(1 / p, 2) if p > 0.01 else None})
    # draw-no-bet and whole-number handicaps (stake back on a push)
    groups["double"].append(_push_market(m, "home", 0, "Draw No Bet: Home"))
    groups["double"].append(_push_market(m, "away", 0, "Draw No Bet: Away"))
    for side in ("home", "away"):
        for line in (-1, 1):
            groups["handicap"].append(_push_market(m, side, line, f"{side.title()} {_fmt(line)} handicap"))

    # Safest options: one best market per group inside the [MARKET_MIN, MARKET_MAX] band
    top = []
    for items in groups.values():
        ok = [i for i in items if "push" not in i and MARKET_MIN <= i["prob"] <= MARKET_MAX]
        if ok:
            top.append(max(ok, key=lambda i: i["prob"]))
    top = sorted(top, key=lambda i: -i["prob"])[:4]

    flat = np.argsort(m, axis=None)[::-1][:5]
    scores = [{"score": f"{i}-{j}", "prob": round(float(m[i, j]), 4)} for i, j in (divmod(int(f), MAXG + 1) for f in flat)]
    return {"xg": [round(lh, 2), round(la, 2)], "total_xg": round(lh + la, 2),
            "groups": groups, "top": top, "scores": scores}
