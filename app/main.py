import logging
from pathlib import Path
import re
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import config
from app.config import DRIVE_PARENT_FOLDER_ID, GITHUB_REPO, GITHUB_WORKFLOW_FILE
from app.drive_client import DriveClient
from app.github_client import GitHubActionsClient
from app.security import AccessGuard, validate_player_username
from app.warehouse import LichessWarehouse

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Lichess Cloud Pipeline & Analytics Portal",
    description="Self-sustaining web portal to trigger Lichess GitHub Actions pipelines, sync Google Drive CSV reports, and explore real-time SSIS-equivalent chess analytics.",
    version="1.2.0",
)

# SEC-04 fix: Do not combine wildcard allow_origins=["*"] with allow_credentials=True.
_cors_origins = [
    o.strip() for o in (config.CORS_ALLOWED_ORIGINS or "").split(",") if o.strip()
]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=("*" not in _cors_origins),
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-Portal-Auth"],
    )


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self';",
    )
    return response


drive_client = DriveClient()
github_client = GitHubActionsClient()
warehouse = LichessWarehouse(drive_client=drive_client)
access_guard = AccessGuard()

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

SAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9_-]")


def _sanitize_header_token(val: str, fallback: str = "all") -> str:
    cleaned = SAFE_TOKEN_RE.sub("", (val or "").strip())
    return cleaned[:32] if cleaned else fallback


class TriggerPipelineRequest(BaseModel):
    username: str


class AuthVerifyRequest(BaseModel):
    token: str


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
        "trigger_protected": access_guard.is_trigger_protected(),
        "view_protected": access_guard.is_view_protected(),
    }


@app.get("/api/auth/status")
def auth_status(request: Request):
    return access_guard.get_status(request)


@app.post("/api/auth/verify")
def auth_verify(req: AuthVerifyRequest):
    token = req.token.strip()
    trigger_user = access_guard.authenticate_trigger_token(token)
    view_ok = access_guard.authenticate_view_token(token)

    if access_guard.is_trigger_protected() and trigger_user is None and not view_ok:
        raise HTTPException(status_code=401, detail="Invalid access key or passcode.")
    if access_guard.is_view_protected() and not view_ok:
        raise HTTPException(status_code=401, detail="Invalid access key or passcode.")

    return {
        "ok": True,
        "authorized_user": trigger_user or ("viewer" if view_ok else "open"),
        "trigger_authorized": (not access_guard.is_trigger_protected())
        or (trigger_user is not None),
        "view_authorized": view_ok,
    }


@app.get("/api/cache/stats")
def cache_stats(request: Request):
    access_guard.require_view_access(request)
    return warehouse.get_cache_stats()


@app.get("/api/players")
def list_players(request: Request, response: Response, refresh: bool = Query(False)):
    access_guard.require_view_access(request)
    try:
        players = warehouse.discover_players(force_refresh=refresh)
        if not refresh:
            scope = "private" if access_guard.is_view_protected() else "public"
            response.headers["Cache-Control"] = f"{scope}, max-age=180"
        else:
            response.headers["Cache-Control"] = "no-store"
        return {
            "players": players,
            "total_players": len(players),
            "drive_folder_id": DRIVE_PARENT_FOLDER_ID,
            "cache_stats": warehouse.get_cache_stats(),
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Error listing players")
        raise HTTPException(
            status_code=500, detail="Failed to list players from warehouse."
        ) from exc


@app.post("/api/sync/{username}")
def sync_player_endpoint(request: Request, username: str, force: bool = Query(True)):
    access_guard.require_view_access(request)
    clean_user = validate_player_username(username, allow_all=True)
    try:
        if clean_user == "__ALL__":
            players = warehouse.discover_players(force_refresh=True)
            results = []
            for p in players:
                results.append(warehouse.sync_player(p["username"], force=force))
            return {"status": "synced_all", "synced": results}

        res = warehouse.sync_player(clean_user, force=force)
        return {"status": "synced", "player": res}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Error syncing player %s", clean_user)
        raise HTTPException(
            status_code=500, detail=f"Failed to sync player '{clean_user}'."
        ) from exc


@app.get("/api/dashboard/{username}")
def get_dashboard(
    request: Request,
    response: Response,
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
    access_guard.require_view_access(request)
    clean_user = validate_player_username(username, allow_all=True)
    try:
        data = warehouse.get_dashboard_data(
            username=clean_user,
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
        if not force_sync:
            scope = "private" if access_guard.is_view_protected() else "public"
            response.headers["Cache-Control"] = f"{scope}, max-age=180"
        else:
            response.headers["Cache-Control"] = "no-store"
        return data
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Error loading dashboard for %s", clean_user)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to compute dashboard analytics for '{clean_user}'.",
        ) from exc


@app.get("/api/export/{username}")
def export_csv(
    request: Request,
    username: str,
    color: str = Query("all"),
    speed: str = Query("all"),
    rated: str = Query("all"),
    source: str = Query("all"),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
):
    access_guard.require_view_access(request)
    clean_user = validate_player_username(username, allow_all=True)
    try:
        csv_text = warehouse.export_filtered_csv(
            username=clean_user,
            color=color,
            speed=speed,
            rated=rated,
            source=source,
            date_from=date_from,
            date_to=date_to,
            search=search,
        )
        safe_name = (
            "all_players"
            if clean_user == "__ALL__"
            else _sanitize_header_token(clean_user, "player")
        )
        safe_color = _sanitize_header_token(color, "all")
        safe_speed = _sanitize_header_token(speed, "all")
        filename = f"lichess_{safe_name}_{safe_color}_{safe_speed}.csv"
        return PlainTextResponse(
            content=csv_text,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Error exporting CSV for %s", clean_user)
        raise HTTPException(
            status_code=500, detail="Failed to export filtered CSV."
        ) from exc


@app.post("/api/pipeline/trigger")
def trigger_pipeline(request: Request, req: TriggerPipelineRequest):
    clean_user = validate_player_username(req.username, allow_all=False)
    caller_label = access_guard.require_trigger_access(request, clean_user)
    succeeded = False

    try:
        try:
            profile = github_client.fetch_lichess_user_profile(clean_user)
            canonical_username = validate_player_username(
                profile.get("username") or clean_user, allow_all=False
            )
        except ValueError as verr:
            raise HTTPException(status_code=404, detail=str(verr)) from verr
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning(
                "Lichess profile check encountered transient error for %s: %s",
                clean_user,
                exc,
            )
            canonical_username = clean_user

        try:
            res = github_client.trigger_workflow(canonical_username)
            res["triggered_by"] = caller_label
            succeeded = True
            return res
        except Exception as exc:
            logger.exception("Failed to trigger workflow for %s", canonical_username)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to dispatch GitHub Actions workflow for '{canonical_username}'.",
            ) from exc
    finally:
        access_guard.release_trigger_slot(request, clean_user, succeeded=succeeded)


@app.get("/api/pipeline/runs")
def list_pipeline_runs(request: Request, limit: int = Query(8, ge=1, le=25)):
    access_guard.require_view_access(request)
    try:
        runs = github_client.list_runs(limit=limit)
        return {"runs": runs, "repo": GITHUB_REPO, "workflow": GITHUB_WORKFLOW_FILE}
    except Exception as exc:
        logger.exception("Error listing pipeline runs")
        raise HTTPException(
            status_code=500, detail="Failed to retrieve workflow runs."
        ) from exc


@app.get("/api/pipeline/runs/{run_id}")
def get_pipeline_run_detail(
    request: Request, run_id: int, auto_sync: bool = Query(True)
):
    access_guard.require_view_access(request)
    try:
        detail = github_client.get_run_detail(run_id)
        if (
            auto_sync
            and detail.get("status") == "completed"
            and detail.get("conclusion") == "success"
            and not detail.get("synced_after_completion")
        ):
            # Single-flight lock so concurrent 3s polls never trigger duplicate Drive syncs (CONC-02)
            if github_client.try_begin_run_sync(run_id):
                target_user = detail.get("username")
                sync_ok = False
                try:
                    warehouse.discover_players(force_refresh=True)
                    if target_user:
                        clean_target = validate_player_username(
                            target_user, allow_all=False
                        )
                        sync_res = warehouse.sync_player(clean_target, force=True)
                        detail["synced_after_completion"] = True
                        detail["sync_result"] = sync_res
                    sync_ok = True
                except Exception as sync_exc:
                    logger.warning(
                        "Post-run auto-sync error for run %s: %s", run_id, sync_exc
                    )
                    detail["sync_error"] = "Auto-sync encountered an error."
                finally:
                    github_client.finish_run_sync(
                        run_id, username=target_user, succeeded=sync_ok
                    )

        return detail
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Error retrieving run detail for %s", run_id)
        raise HTTPException(
            status_code=500, detail="Failed to retrieve workflow run details."
        ) from exc


@app.get("/api/lichess/profile/{username}")
def get_lichess_profile(request: Request, username: str):
    access_guard.require_view_access(request)
    clean_user = validate_player_username(username, allow_all=False)
    try:
        return github_client.fetch_lichess_user_profile(clean_user)
    except ValueError as verr:
        raise HTTPException(status_code=404, detail=str(verr)) from verr
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Error fetching Lichess profile for %s", clean_user)
        raise HTTPException(
            status_code=502, detail="Failed to reach Lichess profile API."
        ) from exc
