import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
MODEL_DIR = BASE_DIR / "models"
DATA_DIR.mkdir(exist_ok=True)
MODEL_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "betting.db"

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()
ODDS_REGIONS = os.getenv("ODDS_REGIONS", "uk,eu")
ODDS_CACHE_TTL = int(os.getenv("ODDS_CACHE_TTL", "7200"))  # seconds; protects free quota
HIST_SEASONS = int(os.getenv("HIST_SEASONS", "5"))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
# Optional Postgres URL (e.g. free Neon) so the track record survives restarts
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# Pick rules (see picks.py)
SINGLE_MIN = float(os.getenv("SINGLE_MIN", "0.55"))  # model must give a team/draw at least this to pick it alone
DC_MIN = float(os.getenv("DC_MIN", "0.65"))          # best double chance must reach this, else "Skip"

# Share of the model in the final probability (rest comes from bookmaker consensus).
# 0 = market only, 1 = model only. Lower = safer while the model is weaker than the market.
BLEND_WEIGHT = float(os.getenv("BLEND_WEIGHT", "0.35"))

# Extra goal markets: "safest options" band (model probability)
MARKET_MIN = float(os.getenv("MARKET_MIN", "0.70"))
MARKET_MAX = float(os.getenv("MARKET_MAX", "0.85"))  # above this the fair odds are too short to be worth listing

# --- Access control & pricing (see gate.py / pricing.py) ---
SECRET_KEY = os.getenv("SECRET_KEY", "").strip()   # signs unlock + premium tokens. REQUIRED in production.
CONTACT_URL = os.getenv("CONTACT_URL", "").strip() # e.g. your Telegram link, shown on the paywall until online payment is live
UNLOCK_GRACE_HOURS = 3                              # an unlock stays valid until this long after kickoff
FREE_DAILY_UNLOCKS = int(os.getenv("FREE_DAILY_UNLOCKS", "10"))  # matches a free user can open per day (Nigeria time)
ADSTERRA_BANNER_HTML = os.getenv("ADSTERRA_BANNER_HTML", "").strip()  # paste your Adsterra banner code; shown to free users only
PLANS = {                                           # base prices in naira; tier decides what premium unlocks
    "two_weeks": {"days": 14, "ngn": 2000, "tier": "basic"},   # all matches, no ads, small analysis
    "month": {"days": 30, "ngn": 3500, "tier": "full"},        # all matches, no ads, complete analysis
}

# The Odds API sport key -> football-data.co.uk league code
LEAGUES = {
    "soccer_epl": {"name": "Premier League", "code": "E0"},
    "soccer_spain_la_liga": {"name": "La Liga", "code": "SP1"},
    "soccer_germany_bundesliga": {"name": "Bundesliga", "code": "D1"},
    "soccer_italy_serie_a": {"name": "Serie A", "code": "I1"},
    "soccer_france_ligue_one": {"name": "Ligue 1", "code": "F1"},
    # --- more leagues (all supported by football-data.co.uk) ---
    "soccer_efl_champ": {"name": "England Championship", "code": "E1"},
    "soccer_england_league1": {"name": "England League One", "code": "E2"},
    "soccer_spl": {"name": "Scotland Premiership", "code": "SC0"},
    "soccer_netherlands_eredivisie": {"name": "Netherlands Eredivisie", "code": "N1"},
    "soccer_portugal_primeira_liga": {"name": "Portugal Primeira Liga", "code": "P1"},
    "soccer_belgium_first_div": {"name": "Belgium Pro League", "code": "B1"},
    "soccer_turkey_super_league": {"name": "Turkey Super Lig", "code": "T1"},
    "soccer_greece_super_league": {"name": "Greece Super League", "code": "G1"},
    "soccer_germany_bundesliga2": {"name": "Bundesliga 2", "code": "D2"},
    "soccer_italy_serie_b": {"name": "Serie B", "code": "I2"},
    "soccer_spain_segunda_division": {"name": "La Liga 2", "code": "SP2"},
    "soccer_france_ligue_two": {"name": "Ligue 2", "code": "F2"},
}
CODE_TO_SPORT = {v["code"]: k for k, v in LEAGUES.items()}

# --- Payments (Flutterwave) & email. Secrets live ONLY in Render's Environment settings, never in GitHub. ---
FLW_SECRET_KEY = os.getenv("FLW_SECRET_KEY", "").strip()      # test key (FLWSECK_TEST-...) first, live key later
FLW_SECRET_HASH = os.getenv("FLW_SECRET_HASH", "").strip()    # the webhook secret hash YOU choose in the Flutterwave dashboard
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")  # e.g. https://edge-analytics-8z95.onrender.com
MAIL_PROVIDER = os.getenv("MAIL_PROVIDER", "").strip().lower()  # "resend" or "brevo" (leave empty = no emails)
MAIL_API_KEY = os.getenv("MAIL_API_KEY", "").strip()
MAIL_FROM = os.getenv("MAIL_FROM", "").strip()                  # a sender address verified with your mail provider
MAIL_FROM_NAME = os.getenv("MAIL_FROM_NAME", "Edge Analytics").strip()
