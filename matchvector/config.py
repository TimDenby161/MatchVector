import os

from dotenv import load_dotenv

load_dotenv()

API_BASE_URL = "https://v3.football.api-sports.io"
API_KEY = os.getenv("API_FOOTBALL_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
API_DAILY_RESERVE = int(os.getenv("API_DAILY_RESERVE", "200"))

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
    # Other European leagues
    141: "Spain Segunda Division",
    179: "Scotland Premiership",
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
}

# API-Football seasons are keyed by the year the season starts (2025 = 2025/26).
DEFAULT_SEASONS = list(range(2020, 2027))

FINISHED_STATUSES = ("FT", "AET", "PEN")

# Odds markets to keep (API-Football bet ids). Everything else is discarded.
ODDS_BET_IDS = {
    1: "Match Winner",
    5: "Goals Over/Under",
    8: "Both Teams Score",
    12: "Double Chance",
}
