import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import quote

import requests

from app.config import (
    DATA_CACHE_DIR,
    GITHUB_BRANCH,
    GITHUB_REPO,
    GITHUB_TOKEN,
    GITHUB_WORKFLOW_FILE,
    LICHESS_TOKEN,
)

logger = logging.getLogger(__name__)

RUN_HISTORY_FILE = DATA_CACHE_DIR / "triggered_runs.json"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{2,30}$")


def _validate_username(username: str) -> str:
    cleaned = (username or "").strip()
    if not USERNAME_RE.fullmatch(cleaned):
        raise ValueError(
            f"Invalid Lichess username '{username}'. Use 2-30 alphanumeric, '_', or '-' characters."
        )
    return cleaned


class GitHubActionsClient:
    """Thread-safe client for triggering and monitoring the Lichess GitHub Actions pipeline."""

    def __init__(
        self,
        repo: str = GITHUB_REPO,
        workflow_file: str = GITHUB_WORKFLOW_FILE,
        branch: str = GITHUB_BRANCH,
    ):
        self.repo = repo
        self.workflow_file = workflow_file
        self.branch = branch
        self._lock = threading.Lock()
        self._syncing_runs: Set[int] = set()
        self._run_map: Dict[str, Any] = self._load_run_map()

    def _load_run_map(self) -> Dict[str, Any]:
        if RUN_HISTORY_FILE.exists():
            try:
                data = json.loads(RUN_HISTORY_FILE.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except Exception as exc:
                logger.warning("Failed to load %s: %s", RUN_HISTORY_FILE, exc)
                return {}
        return {}

    def _save_run_map_unlocked(self) -> None:
        try:
            RUN_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_str = tempfile.mkstemp(
                prefix=".triggered_runs.",
                suffix=".tmp",
                dir=str(RUN_HISTORY_FILE.parent),
            )
            os.close(fd)
            tmp_path = Path(tmp_str)
            try:
                tmp_path.write_text(
                    json.dumps(self._run_map, indent=2), encoding="utf-8"
                )
                tmp_path.replace(RUN_HISTORY_FILE)
            finally:
                if tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except Exception:
                        pass
        except Exception as exc:
            logger.warning("Failed to save %s: %s", RUN_HISTORY_FILE, exc)

    @property
    def has_gh_cli(self) -> bool:
        return shutil.which("gh") is not None

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if GITHUB_TOKEN:
            headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
        return headers

    def trigger_workflow(self, username: str) -> Dict[str, Any]:
        """Trigger workflow_dispatch for the given Lichess username and return run metadata."""
        username = _validate_username(username)

        existing_runs = self.list_runs(limit=5)
        known_ids = {r["id"] for r in existing_runs if r.get("id") is not None}
        trigger_ts = datetime.now(timezone.utc).isoformat()

        dispatched = False
        last_error = None

        # Method 1: GitHub REST API if GITHUB_TOKEN is explicitly configured
        if GITHUB_TOKEN and not GITHUB_TOKEN.startswith("agy-"):
            url = f"https://api.github.com/repos/{self.repo}/actions/workflows/{self.workflow_file}/dispatches"
            resp = requests.post(
                url,
                headers=self._headers(),
                json={"ref": self.branch, "inputs": {"username": username}},
                timeout=20,
            )
            if resp.status_code in (200, 201, 204):
                dispatched = True
            else:
                last_error = f"GitHub API HTTP {resp.status_code}: {resp.text}"

        # Method 2: Official gh CLI
        if not dispatched and self.has_gh_cli:
            cmd = [
                "gh",
                "workflow",
                "run",
                self.workflow_file,
                "-R",
                self.repo,
                "--ref",
                self.branch,
                "-f",
                f"username={username}",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
            if res.returncode == 0:
                dispatched = True
            else:
                last_error = res.stderr.strip() or res.stdout.strip()

        if not dispatched:
            raise RuntimeError(
                f"Failed to trigger GitHub Action for '{username}': {last_error}"
            )

        # Poll briefly (up to 4s) for GitHub to register the new workflow run ID
        detected_run = None
        for _ in range(4):
            time.sleep(1.0)
            recent = self._fetch_raw_runs(limit=5)
            for candidate in recent:
                cid = candidate.get("id")
                if cid is not None and cid not in known_ids:
                    with self._lock:
                        if str(cid) not in self._run_map:
                            detected_run = candidate
                            self._run_map[str(cid)] = {
                                "username": username,
                                "triggered_at": trigger_ts,
                                "synced_after_completion": False,
                            }
                            self._save_run_map_unlocked()
                            break
            if detected_run:
                break

        if detected_run:
            detected_run["username"] = username
            detected_run["synced_after_completion"] = False
            return {
                "status": "dispatched",
                "username": username,
                "triggered_at": trigger_ts,
                "run": detected_run,
            }

        # Queue pending dispatch in a FIFO list so concurrent triggers don't overwrite each other (BUG-04)
        with self._lock:
            pending_list = self._run_map.get("pending_queue")
            if not isinstance(pending_list, list):
                pending_list = []
            pending_list.append(
                {
                    "username": username,
                    "triggered_at": trigger_ts,
                    "known_ids": list(known_ids),
                }
            )
            self._run_map["pending_queue"] = pending_list[-20:]
            self._save_run_map_unlocked()

        return {
            "status": "dispatched",
            "username": username,
            "triggered_at": trigger_ts,
            "run": None,
        }

    def _fetch_raw_runs(self, limit: int = 10) -> List[Dict[str, Any]]:
        raw_runs: List[Dict[str, Any]] = []

        if GITHUB_TOKEN and not GITHUB_TOKEN.startswith("agy-"):
            try:
                url = f"https://api.github.com/repos/{self.repo}/actions/workflows/{self.workflow_file}/runs"
                resp = requests.get(
                    url,
                    headers=self._headers(),
                    params={"per_page": limit},
                    timeout=15,
                )
                if resp.status_code == 200:
                    for r in resp.json().get("workflow_runs", []):
                        raw_runs.append(
                            {
                                "id": r.get("id"),
                                "workflowName": r.get("name"),
                                "displayTitle": r.get("display_title"),
                                "status": r.get("status"),
                                "conclusion": r.get("conclusion"),
                                "event": r.get("event"),
                                "createdAt": r.get("created_at"),
                                "updatedAt": r.get("updated_at"),
                                "url": r.get("html_url"),
                            }
                        )
            except Exception as exc:
                logger.warning("GitHub REST list_runs failed: %s", exc)

        if not raw_runs and self.has_gh_cli:
            cmd = [
                "gh",
                "run",
                "list",
                "-R",
                self.repo,
                "--workflow",
                self.workflow_file,
                "--limit",
                str(limit),
                "--json",
                "databaseId,workflowName,displayTitle,status,conclusion,event,createdAt,updatedAt,url",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            if res.returncode == 0 and res.stdout.strip():
                items = json.loads(res.stdout)
                for item in items:
                    raw_runs.append(
                        {
                            "id": item.get("databaseId"),
                            "workflowName": item.get("workflowName"),
                            "displayTitle": item.get("displayTitle"),
                            "status": item.get("status"),
                            "conclusion": item.get("conclusion"),
                            "event": item.get("event"),
                            "createdAt": item.get("createdAt"),
                            "updatedAt": item.get("updatedAt"),
                            "url": item.get("url"),
                        }
                    )
        return raw_runs

    def list_runs(self, limit: int = 10) -> List[Dict[str, Any]]:
        """List recent runs of the Lichess Data Pipeline workflow."""
        raw_runs = self._fetch_raw_runs(limit=limit)

        with self._lock:
            dirty = False
            # Legacy pending_latest migration
            legacy_pending = self._run_map.pop("pending_latest", None)
            pending_queue: List[Dict[str, Any]] = self._run_map.get("pending_queue", [])
            if not isinstance(pending_queue, list):
                pending_queue = []
            if isinstance(legacy_pending, dict) and legacy_pending.get("username"):
                pending_queue.insert(
                    0,
                    {
                        "username": legacy_pending["username"],
                        "triggered_at": legacy_pending.get("triggered_at"),
                        "known_ids": [legacy_pending.get("previous_top_id")],
                    },
                )
                dirty = True

            if pending_queue and raw_runs:
                # Match oldest unattributed runs to FIFO pending queue entries
                unattributed = [
                    r
                    for r in reversed(raw_runs)
                    if r.get("id") is not None and str(r["id"]) not in self._run_map
                ]
                remaining_queue = []
                for p_entry in pending_queue:
                    known_set = set(p_entry.get("known_ids") or [])
                    matched_run = None
                    for cand in unattributed:
                        if cand["id"] not in known_set and str(cand["id"]) not in self._run_map:
                            matched_run = cand
                            break
                    if matched_run is not None:
                        self._run_map[str(matched_run["id"])] = {
                            "username": p_entry["username"],
                            "triggered_at": p_entry.get("triggered_at"),
                            "synced_after_completion": False,
                        }
                        dirty = True
                    else:
                        remaining_queue.append(p_entry)
                if len(remaining_queue) != len(pending_queue):
                    self._run_map["pending_queue"] = remaining_queue
                    dirty = True

            if dirty:
                self._save_run_map_unlocked()

            for run in raw_runs:
                rid = str(run["id"])
                meta = self._run_map.get(rid, {})
                if isinstance(meta, dict):
                    run["username"] = meta.get("username")
                    run["synced_after_completion"] = meta.get(
                        "synced_after_completion", False
                    )
                else:
                    run["username"] = None
                    run["synced_after_completion"] = False

        return raw_runs

    def get_run_detail(self, run_id: int) -> Dict[str, Any]:
        """Get step-by-step live status for a specific workflow run."""
        rid_str = str(int(run_id))
        with self._lock:
            meta = dict(self._run_map.get(rid_str, {}))

        if self.has_gh_cli:
            cmd = [
                "gh",
                "run",
                "view",
                rid_str,
                "-R",
                self.repo,
                "--json",
                "databaseId,workflowName,displayTitle,status,conclusion,event,createdAt,updatedAt,url,jobs",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            if res.returncode == 0 and res.stdout.strip():
                data = json.loads(res.stdout)
                data["id"] = data.pop("databaseId", run_id)
                data["username"] = meta.get("username")
                data["synced_after_completion"] = meta.get(
                    "synced_after_completion", False
                )
                return data

        if GITHUB_TOKEN:
            r_url = f"https://api.github.com/repos/{self.repo}/actions/runs/{run_id}"
            j_url = (
                f"https://api.github.com/repos/{self.repo}/actions/runs/{run_id}/jobs"
            )
            r_resp = requests.get(r_url, headers=self._headers(), timeout=15)
            j_resp = requests.get(j_url, headers=self._headers(), timeout=15)
            r_resp.raise_for_status()
            r_data = r_resp.json()
            j_data = j_resp.json().get("jobs", []) if j_resp.status_code == 200 else []
            return {
                "id": r_data.get("id"),
                "workflowName": r_data.get("name"),
                "displayTitle": r_data.get("display_title"),
                "status": r_data.get("status"),
                "conclusion": r_data.get("conclusion"),
                "createdAt": r_data.get("created_at"),
                "updatedAt": r_data.get("updated_at"),
                "url": r_data.get("html_url"),
                "username": meta.get("username"),
                "synced_after_completion": meta.get("synced_after_completion", False),
                "jobs": j_data,
            }

        raise RuntimeError("Unable to retrieve workflow run details.")

    def try_begin_run_sync(self, run_id: int) -> bool:
        """
        Ensures only one polling request can trigger auto-sync for a completed run ID (CONC-02).
        """
        rid_int = int(run_id)
        rid_str = str(rid_int)
        with self._lock:
            meta = self._run_map.get(rid_str, {})
            if isinstance(meta, dict) and meta.get("synced_after_completion"):
                return False
            if rid_int in self._syncing_runs:
                return False
            self._syncing_runs.add(rid_int)
            return True

    def finish_run_sync(
        self, run_id: int, username: Optional[str] = None, succeeded: bool = True
    ) -> None:
        rid_int = int(run_id)
        rid_str = str(rid_int)
        with self._lock:
            self._syncing_runs.discard(rid_int)
            if succeeded:
                if rid_str not in self._run_map or not isinstance(
                    self._run_map[rid_str], dict
                ):
                    self._run_map[rid_str] = {"username": username}
                self._run_map[rid_str]["synced_after_completion"] = True
                if username:
                    self._run_map[rid_str]["username"] = username
                self._save_run_map_unlocked()

    def mark_run_synced(self, run_id: int, username: Optional[str] = None) -> None:
        self.finish_run_sync(run_id, username=username, succeeded=True)

    @staticmethod
    def fetch_lichess_user_profile(username: str) -> Dict[str, Any]:
        """Query public Lichess API to verify a player and inspect their live ratings."""
        clean_user = _validate_username(username)
        headers = {"Accept": "application/json"}
        if LICHESS_TOKEN:
            headers["Authorization"] = f"Bearer {LICHESS_TOKEN}"
        resp = requests.get(
            f"https://lichess.org/api/user/{quote(clean_user, safe='')}",
            headers=headers,
            timeout=10,
        )
        if resp.status_code == 404:
            raise ValueError(f"Lichess user '{clean_user}' not found.")
        resp.raise_for_status()
        data = resp.json()
        if not data or data.get("disabled"):
            raise ValueError(f"Lichess user '{clean_user}' is closed or disabled.")
        perfs = data.get("perfs", {})
        ratings = {}
        for mode in (
            "bullet",
            "blitz",
            "rapid",
            "classical",
            "correspondence",
            "puzzle",
        ):
            if mode in perfs and "rating" in perfs[mode]:
                ratings[mode] = {
                    "rating": perfs[mode].get("rating"),
                    "games": perfs[mode].get("games", 0),
                    "prog": perfs[mode].get("prog", 0),
                }
        return {
            "id": data.get("id"),
            "username": data.get("username") or clean_user,
            "title": data.get("title"),
            "online": data.get("online", False),
            "createdAt": data.get("createdAt"),
            "seenAt": data.get("seenAt"),
            "count": data.get("count", {}),
            "ratings": ratings,
            "url": data.get("url", f"https://lichess.org/@/{clean_user}"),
        }
