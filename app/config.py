import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_CACHE_DIR = Path(os.getenv("DATA_CACHE_DIR", str(BASE_DIR / "data_cache")))
DATA_CACHE_DIR.mkdir(parents=True, exist_ok=True)

WAREHOUSE_DB_PATH = DATA_CACHE_DIR / "warehouse.db"

# Google Drive configuration
DRIVE_PARENT_FOLDER_ID = os.getenv(
    "DRIVE_PARENT_FOLDER_ID", "12PIh1S55cgjBMLpJ4G1cFJb-lAxHikqg"
)
GOOGLE_APPLICATION_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
GOOGLE_DRIVE_CREDENTIALS_B64 = os.getenv("GOOGLE_DRIVE_CREDENTIALS")

# GitHub configuration for triggering workflow_dispatch
GITHUB_REPO = os.getenv(
    "GITHUB_REPO", "ChukwumaKingsley/lichess_report_preprocessor"
)
GITHUB_WORKFLOW_FILE = os.getenv("GITHUB_WORKFLOW_FILE", "lichess_pipeline.yaml")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")

# Optional Lichess API token
LICHESS_TOKEN = os.getenv("LICHESS_TOKEN")

# ---------------------------------------------------------------------------
# Public Deployment Protection & Access Control
# ---------------------------------------------------------------------------
# Optional passcode(s) required to trigger GitHub Actions (comma-separated)
PORTAL_TRIGGER_PASSCODE = os.getenv("PORTAL_TRIGGER_PASSCODE", "").strip()

# Optional per-user tokens in "alice:key1,bob:key2" format for subset access
PORTAL_USER_TOKENS = os.getenv("PORTAL_USER_TOKENS", "").strip()

# Optional passcode required to view dashboards at all
PORTAL_VIEW_PASSCODE = os.getenv("PORTAL_VIEW_PASSCODE", "").strip()

# Optional comma-separated Lichess usernames allowed to be triggered publicly
ALLOWED_TRIGGER_PLAYERS = os.getenv("ALLOWED_TRIGGER_PLAYERS", "").strip()

# Rate-limiting defaults to protect GitHub Actions quota
TRIGGER_COOLDOWN_SECONDS = int(os.getenv("TRIGGER_COOLDOWN_SECONDS", "120"))
TRIGGER_MAX_PER_HOUR = int(os.getenv("TRIGGER_MAX_PER_HOUR", "10"))

# ---------------------------------------------------------------------------
# Multi-Layer Caching TTLs (Reduce Google Drive API calls & recomputation)
# ---------------------------------------------------------------------------
DRIVE_FOLDER_CACHE_TTL = int(os.getenv("DRIVE_FOLDER_CACHE_TTL", "300"))
PLAYER_SYNC_TTL_SECONDS = int(os.getenv("PLAYER_SYNC_TTL_SECONDS", "600"))
DASHBOARD_CACHE_TTL_SECONDS = int(os.getenv("DASHBOARD_CACHE_TTL_SECONDS", "300"))

