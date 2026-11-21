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
ODDS_CACHE_TTL = int(os.getenv("ODDS_CACHE_TTL", "900"))  # seconds; protects free quota
HIST_SEASONS = int(os.getenv("HIST_SEASONS", "5"))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

# The Odds API sport key -> football-data.co.uk league code
LEAGUES = {
    "soccer_epl": {"name": "Premier League", "code": "E0"},
    "soccer_spain_la_liga": {"name": "La Liga", "code": "SP1"},
    "soccer_germany_bundesliga": {"name": "Bundesliga", "code": "D1"},
    "soccer_italy_serie_a": {"name": "Serie A", "code": "I1"},
    "soccer_france_ligue_one": {"name": "Ligue 1", "code": "F1"},
}
CODE_TO_SPORT = {v["code"]: k for k, v in LEAGUES.items()}
