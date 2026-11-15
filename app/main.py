from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import DRIVE_PARENT_FOLDER_ID, GITHUB_REPO, GITHUB_WORKFLOW_FILE
from app.drive_client import DriveClient
from app.github_client import GitHubActionsClient
from app.warehouse import LichessWarehouse

app = FastAPI(
    title="Lichess Cloud Pipeline & Analytics Portal",
    description="Self-sustaining web portal to trigger Lichess GitHub Actions pipelines, sync Google Drive CSV reports, and explore real-time SSIS-equivalent chess analytics.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

drive_client = DriveClient()
github_client = GitHubActionsClient()
warehouse = LichessWarehouse(drive_client=drive_client)

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class TriggerPipelineRequest(BaseModel):
    username: str


@app.get("/")
def serve_index():
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        return PlainTextResponse("Frontend loading...", status_code=200)
    return FileResponse(str(index_path))


@app.get("/api/health")
def health_check():
    return {
        "status": "ok",
        "drive_folder_id": DRIVE_PARENT_FOLDER_ID,
        "github_repo": GITHUB_REPO,
        "github_workflow": GITHUB_WORKFLOW_FILE,
    }


@app.get("/api/players")
def list_players(refresh: bool = Query(False)):
    try:
        players = warehouse.discover_players(force_refresh=refresh)
        return {
            "players": players,
            "total_players": len(players),
            "drive_folder_id": DRIVE_PARENT_FOLDER_ID,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/sync/{username}")
def sync_player_endpoint(username: str, force: bool = Query(True)):
    try:
        if username == "__ALL__":
            players = warehouse.discover_players(force_refresh=True)
            results = []
            for p in players:
                results.append(warehouse.sync_player(p["username"], force=force))
            return {"status": "synced_all", "synced": results}

        res = warehouse.sync_player(username, force=force)
        return {"status": "synced", "player": res}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/dashboard/{username}")
def get_dashboard(
    username: str,
    color: str = Query("all"),
    speed: str = Query("all"),
    rated: str = Query("all"),
    source: str = Query("all"),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=5, le=100),
    force_sync: bool = Query(False),
):
    try:
        return warehouse.get_dashboard_data(
            username=username,
            color=color,
            speed=speed,
            rated=rated,
            source=source,
            date_from=date_from,
            date_to=date_to,
            search=search,
            page=page,
            page_size=page_size,
            force_sync=force_sync,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/export/{username}")
def export_csv(
    username: str,
    color: str = Query("all"),
    speed: str = Query("all"),
    rated: str = Query("all"),
    source: str = Query("all"),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
):
    try:
        csv_text = warehouse.export_filtered_csv(
            username=username,
            color=color,
            speed=speed,
            rated=rated,
            source=source,
            date_from=date_from,
            date_to=date_to,
            search=search,
        )
        safe_name = "all_players" if username == "__ALL__" else username
        filename = f"lichess_{safe_name}_{color}_{speed}.csv"
        return PlainTextResponse(
            content=csv_text,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/pipeline/trigger")
def trigger_pipeline(req: TriggerPipelineRequest):
    username = req.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username cannot be empty.")
    try:
        # Validate user exists on Lichess first so we don't waste a workflow run on a typo
        profile = github_client.fetch_lichess_user_profile(username)
        canonical_username = profile.get("username") or username
    except ValueError as verr:
        raise HTTPException(status_code=404, detail=str(verr)) from verr
    except Exception:
        canonical_username = username

    try:
        res = github_client.trigger_workflow(canonical_username)
        return res
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/pipeline/runs")
def list_pipeline_runs(limit: int = Query(8, ge=1, le=25)):
    try:
        runs = github_client.list_runs(limit=limit)
        return {"runs": runs, "repo": GITHUB_REPO, "workflow": GITHUB_WORKFLOW_FILE}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/pipeline/runs/{run_id}")
def get_pipeline_run_detail(run_id: int, auto_sync: bool = Query(True)):
    try:
        detail = github_client.get_run_detail(run_id)
        # If the run just finished with success and hasn't been synced yet, auto-sync Drive!
        if (
            auto_sync
            and detail.get("status") == "completed"
            and detail.get("conclusion") == "success"
            and not detail.get("synced_after_completion")
        ):
            target_user = detail.get("username")
            warehouse.discover_players(force_refresh=True)
            if target_user:
                try:
                    sync_res = warehouse.sync_player(target_user, force=True)
                    github_client.mark_run_synced(run_id, target_user)
                    detail["synced_after_completion"] = True
                    detail["sync_result"] = sync_res
                except Exception as sync_exc:
                    detail["sync_error"] = str(sync_exc)
            else:
                github_client.mark_run_synced(run_id)

        return detail
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/lichess/profile/{username}")
def get_lichess_profile(username: str):
    try:
        return github_client.fetch_lichess_user_profile(username)
    except ValueError as verr:
        raise HTTPException(status_code=404, detail=str(verr)) from verr
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
