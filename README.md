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

## Cloud Deployment (Render / Railway / Docker)

- **Render**: Connect this repository as a Web Service on Render (`render.yaml` is pre-configured). Set `GITHUB_TOKEN` and `GOOGLE_DRIVE_CREDENTIALS` (the same base64 service account string used in your GitHub Actions secrets).
- **Docker**:
  ```bash
  docker build -t lichess-analytics-portal .
  docker run -p 8000:8000 --env-file .env lichess-analytics-portal
  ```
