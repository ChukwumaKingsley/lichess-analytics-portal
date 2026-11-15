import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from app.config import (
    DATA_CACHE_DIR,
    GITHUB_BRANCH,
    GITHUB_REPO,
    GITHUB_TOKEN,
    GITHUB_WORKFLOW_FILE,
    LICHESS_TOKEN,
)

RUN_HISTORY_FILE = DATA_CACHE_DIR / "triggered_runs.json"


class GitHubActionsClient:
    """Client for triggering and monitoring the Lichess GitHub Actions pipeline."""

    def __init__(
        self,
        repo: str = GITHUB_REPO,
        workflow_file: str = GITHUB_WORKFLOW_FILE,
        branch: str = GITHUB_BRANCH,
    ):
        self.repo = repo
        self.workflow_file = workflow_file
        self.branch = branch
        self._run_map: Dict[str, Dict[str, Any]] = self._load_run_map()

    def _load_run_map(self) -> Dict[str, Dict[str, Any]]:
        if RUN_HISTORY_FILE.exists():
            try:
                return json.loads(RUN_HISTORY_FILE.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_run_map(self) -> None:
        try:
            RUN_HISTORY_FILE.write_text(
                json.dumps(self._run_map, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

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
        username = username.strip()
        if not username:
            raise ValueError("Username is required.")

        # Capture existing latest run ID before triggering so we can identify the new run
        existing_runs = self.list_runs(limit=3)
        latest_existing_id = existing_runs[0]["id"] if existing_runs else None
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

        # Method 2: Official gh CLI (pre-configured with OAuth proxy in Antigravity VM)
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

        # Poll briefly (up to ~6s) for GitHub to register the new workflow run ID
        detected_run = None
        for _ in range(4):
            time.sleep(1.5)
            recent = self.list_runs(limit=5)
            if recent:
                top = recent[0]
                if top["id"] != latest_existing_id:
                    detected_run = top
                    break

        if detected_run:
            run_id_str = str(detected_run["id"])
            self._run_map[run_id_str] = {
                "username": username,
                "triggered_at": trigger_ts,
                "synced_after_completion": False,
            }
            self._save_run_map()
            detected_run["username"] = username
            return {
                "status": "dispatched",
                "username": username,
                "triggered_at": trigger_ts,
                "run": detected_run,
            }

        # If GitHub hasn't assigned the run ID within 6s, record pending trigger
        self._run_map["pending_latest"] = {
            "username": username,
            "triggered_at": trigger_ts,
            "previous_top_id": latest_existing_id,
        }
        self._save_run_map()
        return {
            "status": "dispatched",
            "username": username,
            "triggered_at": trigger_ts,
            "run": None,
        }

    def list_runs(self, limit: int = 10) -> List[Dict[str, Any]]:
        """List recent runs of the Lichess Data Pipeline workflow."""
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
            except Exception:
                pass

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

        # Resolve pending_latest if applicable
        pending = self._run_map.get("pending_latest")
        if pending and raw_runs:
            top_id = raw_runs[0]["id"]
            if top_id != pending.get("previous_top_id"):
                self._run_map[str(top_id)] = {
                    "username": pending["username"],
                    "triggered_at": pending["triggered_at"],
                    "synced_after_completion": False,
                }
                del self._run_map["pending_latest"]
                self._save_run_map()

        for run in raw_runs:
            rid = str(run["id"])
            meta = self._run_map.get(rid, {})
            run["username"] = meta.get("username")
            run["synced_after_completion"] = meta.get("synced_after_completion", False)

        return raw_runs

    def get_run_detail(self, run_id: int) -> Dict[str, Any]:
        """Get step-by-step live status for a specific workflow run."""
        rid_str = str(run_id)
        meta = self._run_map.get(rid_str, {})

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

    def mark_run_synced(self, run_id: int, username: Optional[str] = None) -> None:
        rid_str = str(run_id)
        if rid_str not in self._run_map:
            self._run_map[rid_str] = {"username": username}
        self._run_map[rid_str]["synced_after_completion"] = True
        if username:
            self._run_map[rid_str]["username"] = username
        self._save_run_map()

    @staticmethod
    def fetch_lichess_user_profile(username: str) -> Dict[str, Any]:
        """Query public Lichess API to verify a player and inspect their live ratings."""
        headers = {"Accept": "application/json"}
        if LICHESS_TOKEN:
            headers["Authorization"] = f"Bearer {LICHESS_TOKEN}"
        resp = requests.get(
            f"https://lichess.org/api/user/{username.strip()}",
            headers=headers,
            timeout=10,
        )
        if resp.status_code == 404:
            raise ValueError(f"Lichess user '{username}' not found.")
        resp.raise_for_status()
        data = resp.json()
        perfs = data.get("perfs", {})
        ratings = {}
        for mode in ("bullet", "blitz", "rapid", "classical", "correspondence", "puzzle"):
            if mode in perfs and "rating" in perfs[mode]:
                ratings[mode] = {
                    "rating": perfs[mode].get("rating"),
                    "games": perfs[mode].get("games", 0),
                    "prog": perfs[mode].get("prog", 0),
                }
        return {
            "id": data.get("id"),
            "username": data.get("username"),
            "title": data.get("title"),
            "online": data.get("online", False),
            "createdAt": data.get("createdAt"),
            "seenAt": data.get("seenAt"),
            "count": data.get("count", {}),
            "ratings": ratings,
            "url": data.get("url", f"https://lichess.org/@/{username}"),
        }
