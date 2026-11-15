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
