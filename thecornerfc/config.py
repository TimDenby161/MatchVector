import os

from dotenv import load_dotenv

load_dotenv()


def _bool_env(name, default):
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class SafetyError(RuntimeError):
    """Raised when a local safety guard blocks a dangerous action."""


MODE = os.getenv("THECORNERFC_MODE", "local").strip().lower()
GITHUB_ACTIONS = _bool_env("GITHUB_ACTIONS", False)
READ_ONLY = _bool_env("THECORNERFC_READ_ONLY", MODE != "production")
NO_API = _bool_env("THECORNERFC_NO_API", MODE != "production")
LOCAL_OVERRIDE = os.getenv("THECORNERFC_LOCAL_OVERRIDE", "")
LOCAL_OVERRIDE_TOKEN = "I_UNDERSTAND_THIS_CAN_WRITE_PRODUCTION_DATA_AND_USE_API_QUOTA"

API_BASE_URL = "https://v3.football.api-sports.io"
API_KEY = os.getenv("API_FOOTBALL_KEY")
READ_ONLY_DATABASE_URL = os.getenv("READ_ONLY_DATABASE_URL")
DATABASE_URL = READ_ONLY_DATABASE_URL if READ_ONLY else os.getenv("DATABASE_URL")
API_DAILY_RESERVE = int(os.getenv("API_DAILY_RESERVE", "200"))


def _local_override_allowed():
    return LOCAL_OVERRIDE == LOCAL_OVERRIDE_TOKEN


def require_db_write(action):
    if READ_ONLY:
        raise SafetyError(
            f"{action} requires database writes, but THECORNERFC_READ_ONLY is enabled. "
            "Use a read-only command locally, or set THECORNERFC_READ_ONLY=false plus "
            f"THECORNERFC_LOCAL_OVERRIDE={LOCAL_OVERRIDE_TOKEN!r} if you really intend local writes."
        )
    if not GITHUB_ACTIONS and not _local_override_allowed():
        raise SafetyError(
            f"{action} can write production data. Local writes require "
            f"THECORNERFC_LOCAL_OVERRIDE={LOCAL_OVERRIDE_TOKEN!r}."
        )


def require_api_access(action):
    if NO_API:
        raise SafetyError(
            f"{action} requires API-Football access, but THECORNERFC_NO_API is enabled. "
            "This prevents accidental quota use during local development."
        )
    if not GITHUB_ACTIONS and not _local_override_allowed():
        raise SafetyError(
            f"{action} can consume API-Football quota. Local API access requires "
            f"THECORNERFC_LOCAL_OVERRIDE={LOCAL_OVERRIDE_TOKEN!r}."
        )

# API-Football league ids. Play-offs are included in each league's fixtures.
LEAGUES = {
    # England
    39: "Premier League",
    40: "Championship",
    41: "League One",
    42: "League Two",
    43: "National League",
    50: "National League North",
    51: "National League South",
    # UK cups
    45: "FA Cup",
    48: "EFL Cup",
    46: "EFL Trophy",
    47: "FA Trophy",
    528: "Community Shield",
    181: "Scottish Cup",
    185: "Scottish League Cup",
    # European / world club competitions
    2: "UEFA Champions League",
    3: "UEFA Europa League",
    848: "UEFA Conference League",
    531: "UEFA Super Cup",
    15: "FIFA Club World Cup",
    # Big 5 (rest)
    140: "La Liga",
    135: "Serie A",
    136: "Serie B",
    78: "Bundesliga",
    79: "2. Bundesliga",
    61: "Ligue 1",
    62: "Ligue 2",
    # Other European leagues
    141: "Spain Segunda Division",
    179: "Scotland Premiership",
    180: "Scotland Championship",
    88: "Netherlands Eredivisie",
    94: "Portugal Primeira Liga",
    144: "Belgium Pro League",
    203: "Turkey Super Lig",
    197: "Greece Super League 1",
    207: "Switzerland Super League",
    218: "Austria Bundesliga",
    119: "Denmark Superliga",
    103: "Norway Eliteserien",
    113: "Sweden Allsvenskan",
    106: "Poland Ekstraklasa",
    235: "Russia Premier League",
    333: "Ukraine Premier League",
    286: "Serbia Super Liga",
    345: "Czech Liga",
    419: "Azerbaijan Premyer Liqa",
    332: "Slovakia Super Liga",
    271: "Hungary NB I",
    318: "Cyprus 1. Division",
    383: "Israel Ligat Ha'al",
    172: "Bulgaria First League",
    373: "Slovenia 1. SNL",
    342: "Armenia Premier League",
    315: "Bosnia Premijer Liga",
    310: "Albania Superliga",
    362: "Lithuania A Lyga",
    327: "Georgia Erovnuli Liga",
    312: "Andorra 1a Divisio",
    389: "Kazakhstan Premier League",
    244: "Finland Veikkausliiga",
    758: "Gibraltar Premier Division",
    365: "Latvia Virsliga",
    283: "Romania Liga I",
    210: "Croatia HNL",
    # Rest of world
    71: "Brazil Serie A",
    128: "Argentina Liga Profesional",
    262: "Mexico Liga MX",
    307: "Saudi Pro League",
    253: "MLS",
    233: "Egypt Premier League",
    188: "Australia A-League",
}

# API-Football seasons are keyed by the year the season starts (2025 = 2025/26).
DEFAULT_SEASONS = list(range(2020, 2027))

# Leagues with player data and injury lists
PLAYER_LEAGUES = [
    39, 40, 41, 42, 43,            # England top five
    140, 135, 78, 61,              # La Liga, Serie A, Bundesliga, Ligue 1
    203, 307, 253, 94, 88, 144,    # Turkey, Saudi Arabia, MLS, Portugal, Netherlands, Belgium
    197, 333, 345, 218, 103, 419,  # Greece, Ukraine, Czechia, Austria, Norway, Azerbaijan
    332,                           # Slovakia
]

# Leagues with enough injury history (2021+) for the injury adjustment; per-match player
# minutes are fetched for these
INJURY_MODEL_LEAGUES = [39, 40, 140, 135, 78, 61, 203, 88, 253, 103]

# Leagues with per-match player data (fixture_players: minutes, stats, line-up roles), used for
# player ranks and the players list: the injury-model leagues plus League One, League Two and
# the Saudi Pro League
MATCH_PLAYER_LEAGUES = INJURY_MODEL_LEAGUES + [41, 42, 307]

# Player ranks compare every player with the players in these leagues (stat norms, percentiles,
# the "regulars" sample), so adding a league doesn't move everyone else's rank
RATING_REFERENCE_LEAGUES = INJURY_MODEL_LEAGUES

FINISHED_STATUSES = ("FT", "AET", "PEN")

# Odds markets to keep (API-Football bet ids). Everything else is discarded.
ODDS_BET_IDS = {
    1: "Match Winner",
    5: "Goals Over/Under",
    8: "Both Teams Score",
    12: "Double Chance",
}

# Observed daily headers are distinct from any unconfirmed subscription allowance.
API_LEDGER_PATH = os.getenv("API_LEDGER_PATH", ".api-usage/ledger.sqlite3")
API_QUOTA_WARN_THRESHOLDS = sorted({int(n) for n in os.getenv(
    "API_QUOTA_WARN_THRESHOLDS", "2000,1000,500").split(',') if n.strip()}, reverse=True)
API_RUN_BUDGET = int(os.getenv("API_RUN_BUDGET", "0"))  # 0 = no additional cap

if API_RUN_BUDGET < 0:
    raise ValueError("API_RUN_BUDGET must be non-negative")

HEALTH_COLLAPSE_RATIO = float(os.getenv("HEALTH_COLLAPSE_RATIO", "0.2"))
HEALTH_MIN_BASELINE = int(os.getenv("HEALTH_MIN_BASELINE", "100"))
HEALTH_STALE_HOURS = int(os.getenv("HEALTH_STALE_HOURS", "72"))

if not 0 < HEALTH_COLLAPSE_RATIO < 1 or HEALTH_MIN_BASELINE < 1 or HEALTH_STALE_HOURS < 1:
    raise ValueError("Health ratio must be between 0 and 1; baseline and stale hours must be positive")
