import base64
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import (
    DATA_CACHE_DIR,
    DRIVE_PARENT_FOLDER_ID,
    GOOGLE_APPLICATION_CREDENTIALS,
    GOOGLE_DRIVE_CREDENTIALS_B64,
)

logger = logging.getLogger(__name__)

DRIVE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{5,128}$")
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def _escape_drive_q(val: str) -> str:
    return val.replace("\\", "\\\\").replace("'", "\\'")


def _validate_drive_id(item_id: str) -> str:
    cleaned = (item_id or "").strip()
    if not DRIVE_ID_RE.fullmatch(cleaned):
        raise ValueError(f"Invalid Google Drive resource ID: '{item_id}'")
    return cleaned


class DriveClient:
    """
    Thread-safe hybrid Google Drive client supporting both Google Drive API SDK and gdrive CLI.
    Uses thread-local googleapiclient Resources so httplib2 sockets are never shared across threads (CONC-01),
    loads service account credentials in-memory without world-readable key files (SEC-06),
    and uses unique atomic temporary files for downloads (CONC-02).
    """

    def __init__(self, parent_folder_id: str = DRIVE_PARENT_FOLDER_ID):
        self.parent_folder_id = parent_folder_id
        self._credentials = None
        self._sdk_available = False
        self._thread_local = threading.local()
        self._init_credentials()

    def _init_credentials(self) -> None:
        # Remove any legacy plaintext service account file left in DATA_CACHE_DIR
        legacy_file = DATA_CACHE_DIR / ".service_account.json"
        if legacy_file.exists():
            try:
                os.chmod(legacy_file, 0o600)
            except Exception:
                pass

        try:
            raw_creds = (GOOGLE_DRIVE_CREDENTIALS_B64 or "").strip()
            if raw_creds:
                if raw_creds.startswith("{"):
                    creds_info = json.loads(raw_creds)
                else:
                    creds_bytes = base64.b64decode(raw_creds)
                    creds_info = json.loads(creds_bytes.decode("utf-8"))

                from google.oauth2 import service_account

                self._credentials = (
                    service_account.Credentials.from_service_account_info(
                        creds_info, scopes=DRIVE_SCOPES
                    )
                )
                self._sdk_available = True
                return

            cred_path = GOOGLE_APPLICATION_CREDENTIALS or os.getenv(
                "GOOGLE_APPLICATION_CREDENTIALS"
            )
            if cred_path and Path(cred_path).exists():
                from google.oauth2 import service_account

                self._credentials = (
                    service_account.Credentials.from_service_account_file(
                        cred_path, scopes=DRIVE_SCOPES
                    )
                )
                self._sdk_available = True
        except Exception as exc:
            logger.warning(
                "[DriveClient] SDK credentials init skipped, falling back to gdrive CLI: %s",
                exc,
            )
            self._credentials = None
            self._sdk_available = False

    def _get_thread_sdk_service(self):
        if not self._sdk_available or self._credentials is None:
            return None
        svc = getattr(self._thread_local, "sdk_service", None)
        if svc is None:
            from googleapiclient.discovery import build

            svc = build(
                "drive",
                "v3",
                credentials=self._credentials,
                cache_discovery=False,
            )
            self._thread_local.sdk_service = svc
        return svc

    @property
    def has_gdrive_cli(self) -> bool:
        return shutil.which("gdrive") is not None

    def _list_folder_via_cli(self, folder_id: str) -> List[Dict[str, Any]]:
        safe_id = _validate_drive_id(folder_id)
        cmd = ["gdrive", "readonly", "ls", safe_id, "--json", "--max", "500"]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            raise RuntimeError(f"gdrive ls failed: {res.stderr.strip()}")
        out = res.stdout.strip()
        if not out:
            return []
        data = json.loads(out)
        return data if isinstance(data, list) else []

    def _list_folder_via_sdk(self, folder_id: str) -> List[Dict[str, Any]]:
        safe_id = _escape_drive_q(_validate_drive_id(folder_id))
        svc = self._get_thread_sdk_service()
        if svc is None:
            raise RuntimeError("Google Drive SDK service is not initialized.")
        query = f"'{safe_id}' in parents and trashed = false"
        fields = "files(id, name, mimeType, modifiedTime, size)"
        resp = (
            svc.files()
            .list(q=query, fields=fields, pageSize=500)
            .execute()
        )
        return resp.get("files", [])

    def list_folder(self, folder_id: Optional[str] = None) -> List[Dict[str, Any]]:
        target_id = _validate_drive_id(folder_id or self.parent_folder_id)
        if self._sdk_available:
            try:
                return self._list_folder_via_sdk(target_id)
            except Exception as exc:
                logger.warning(
                    "[DriveClient] SDK list_folder failed for %s: %s", target_id, exc
                )
                if self.has_gdrive_cli:
                    return self._list_folder_via_cli(target_id)
                raise exc
        if self.has_gdrive_cli:
            return self._list_folder_via_cli(target_id)
        raise RuntimeError(
            "Neither Google Drive SDK credentials nor gdrive CLI are available."
        )

    def list_player_folders(self) -> List[Dict[str, Any]]:
        items = self.list_folder(self.parent_folder_id)
        folders = [
            item
            for item in items
            if item.get("mimeType") == "application/vnd.google-apps.folder"
        ]
        folders.sort(key=lambda x: x.get("name", "").lower())
        return folders

    def get_player_files_metadata(self, folder_id: str) -> Dict[str, Dict[str, Any]]:
        """Returns a dict mapping filename -> file metadata inside a player's folder."""
        items = self.list_folder(folder_id)
        return {item["name"]: item for item in items if "name" in item}

    def download_file(self, file_id: str, dest_path: Path) -> Path:
        safe_file_id = _validate_drive_id(file_id)
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        fd, tmp_str = tempfile.mkstemp(
            prefix=f".{dest_path.name}.",
            suffix=".tmp",
            dir=str(dest_path.parent),
        )
        os.close(fd)
        tmp_path = Path(tmp_str)

        try:
            if self._sdk_available:
                try:
                    from googleapiclient.http import MediaIoBaseDownload

                    svc = self._get_thread_sdk_service()
                    request = svc.files().get_media(fileId=safe_file_id)
                    fh = io.BytesIO()
                    downloader = MediaIoBaseDownload(fh, request)
                    done = False
                    while not done:
                        _, done = downloader.next_chunk()
                    tmp_path.write_bytes(fh.getvalue())
                    tmp_path.replace(dest_path)
                    return dest_path
                except Exception as exc:
                    logger.warning(
                        "[DriveClient] SDK download_file failed for %s: %s",
                        safe_file_id,
                        exc,
                    )
                    if not self.has_gdrive_cli:
                        raise exc

            if self.has_gdrive_cli:
                if tmp_path.exists():
                    tmp_path.unlink()
                cmd = [
                    "gdrive",
                    "readonly",
                    "download",
                    safe_file_id,
                    "--out",
                    str(tmp_path),
                ]
                res = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=120
                )
                if res.returncode != 0:
                    raise RuntimeError(
                        f"gdrive download failed for {safe_file_id}: {res.stderr.strip()}"
                    )
                tmp_path.replace(dest_path)
                return dest_path

            raise RuntimeError(
                "No method available to download file from Google Drive."
            )
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
