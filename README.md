# Lichess Cloud Pipeline & Real-Time Analytics Portal

A self-sustaining full-stack web application that triggers the Lichess GitHub Actions pipeline (`ChukwumaKingsley/lichess_report_preprocessor`), syncs the generated player CSV reports from Google Drive (`lichess_reports/`), executes automated **SSIS-equivalent transformations** (Unified Multi-Player Warehouse + Conditional Split for White/Black tables), and renders an interactive real-time analytics dashboard remotely.

---

## Architecture Overview

1. **Remote Pipeline Trigger (`app/github_client.py`)**
   - Validates any Lichess username against the live Lichess API (`https://lichess.org/api/user/{username}`).
   - Dispatches `lichess_pipeline.yaml` on `ChukwumaKingsley/lichess_report_preprocessor` with `inputs.username`.
   - Tracks real-time GitHub Actions step-by-step progress (`Set up job` → `Install dependencies` → `Run all scripts` → `Complete job`).
   - Automatically triggers a Google Drive warehouse sync as soon as a workflow run succeeds.

2. **Google Drive Sync & SSIS-Replacement Warehouse (`app/drive_client.py`, `app/warehouse.py`)**
   - Connects to the `lichess_reports/` Google Drive folder (`12PIh1S55cgjBMLpJ4G1cFJb-lAxHikqg`) via Google Drive API v3 (or `gdrive` CLI).
   - Replaces the local SQL Server + SSIS packages (`Looping Through files.dtsx` and `Basic Transformations.dtsx`):
     - **Unified Multi-Player Table (`__ALL__`)**: Aggregates all synced player CSVs into a single queryable warehouse.
     - **Conditional Split (`played_as == "white"` vs `played_as == "black"`)**: Provides instant side-by-side comparison and filtering across All Games, White Table, and Black Table.
     - **CSV Export**: Exports any filtered slice directly to CSV (replacing the SSIS Flat File / Excel destination).

3. **Real-Time Interactive Analytics (`app/static/`)**
   - Executive KPI Strip (Total Games, Win/Draw/Loss split bar, Current & Peak Rating, Net Rating Delta, Strength of Schedule, Tempo/Turns).
   - SSIS Conditional Split Comparison (White vs. Black win rate, rating delta, average turns, top opening).
   - Multi-Category Rating History Progression (`bullet`, `blitz`, `rapid`, `classical`, `puzzle`, variants).
   - Game Termination & Outcome Matrix (`mate`, `resign`, `outoftime`, `draw`, `stalemate`, `timeout`).
   - Monthly Activity Volume & Rolling Win-Rate Evolution.
   - Time-of-Day (24h UTC) & Day-of-Week Performance Heatmap.
   - Opening Repertoire Explorer (ECO & variation analytics as Overall, White, and Black).
   - Speed / Clock Matrix & Frequent Opponent Head-to-Head Rivalries.
   - Paginated Game Log Explorer with direct Lichess board replay links.

---

## Running Locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Populate GITHUB_TOKEN and GOOGLE_DRIVE_CREDENTIALS in .env
uvicorn app.main:app --reload --port 8000
```

## Public Deployment Protection & Subset Access

You can safely expose the portal publicly while protecting your GitHub Actions quota and Drive/Lichess tokens using environment variables:

| Variable | Purpose | Example |
| :--- | :--- | :--- |
| `PORTAL_TRIGGER_PASSCODE` | Shared passcode required to trigger GitHub Actions (dashboards remain viewable) | `my-secret-pass` |
| `PORTAL_USER_TOKENS` | Per-user revocable access keys (`label:key`) for a subset of trusted users | `kingsley:k-123,friend:f-456` |
| `PORTAL_VIEW_PASSCODE` | Optional passcode to lock the entire dashboard behind a login overlay | `view-secret` |
| `ALLOWED_TRIGGER_PLAYERS` | Restrict triggers to an allowlist of Lichess usernames (leave blank for any valid player) | `Wizard-King,Real_Vick` |
| `TRIGGER_COOLDOWN_SECONDS` | Per-player cooldown between dispatches (default: `120`) | `120` |
| `TRIGGER_MAX_PER_HOUR` | Max workflow triggers per IP per hour (default: `10`) | `10` |

## Cloud Deployment (Render / Railway / Docker)

- **One-Click Render Blueprint**: Open [`https://render.com/deploy?repo=https://github.com/ChukwumaKingsley/lichess-analytics-portal`](https://render.com/deploy?repo=https://github.com/ChukwumaKingsley/lichess-analytics-portal) (`render.yaml` is pre-configured with `/api/health`, Python 3.11, and all environment variables).
  - Required secrets to paste into Render during setup:
    1. `GOOGLE_DRIVE_CREDENTIALS`: The same base64-encoded service account JSON stored in your `lichess_report_preprocessor` GitHub Actions secrets.
    2. `GITHUB_TOKEN`: A GitHub Personal Access Token (with `workflow` / Actions read+write access on `ChukwumaKingsley/lichess_report_preprocessor`).
  - Optional protection variables: `PORTAL_USER_TOKENS`, `PORTAL_TRIGGER_PASSCODE`, `ALLOWED_TRIGGER_PLAYERS`, `PORTAL_VIEW_PASSCODE`.
- **Docker**:
  ```bash
  docker build -t lichess-analytics-portal .
  docker run -p 8000:8000 --env-file .env lichess-analytics-portal
  ```
