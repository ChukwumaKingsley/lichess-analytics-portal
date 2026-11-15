import base64
import io
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import (
    DATA_CACHE_DIR,
    DRIVE_PARENT_FOLDER_ID,
    GOOGLE_APPLICATION_CREDENTIALS,
    GOOGLE_DRIVE_CREDENTIALS_B64,
)


class DriveClient:
    """Hybrid Google Drive client supporting both Google Drive API SDK and gdrive CLI."""

    def __init__(self, parent_folder_id: str = DRIVE_PARENT_FOLDER_ID):
        self.parent_folder_id = parent_folder_id
        self._sdk_service = None
        self._init_sdk_if_available()

    def _init_sdk_if_available(self) -> None:
        try:
            raw_creds = (GOOGLE_DRIVE_CREDENTIALS_B64 or "").strip()
            if raw_creds and not GOOGLE_APPLICATION_CREDENTIALS:
                if raw_creds.startswith("{"):
                    creds_bytes = raw_creds.encode("utf-8")
                else:
                    creds_bytes = base64.b64decode(raw_creds)
                tmp_creds = DATA_CACHE_DIR / ".service_account.json"
                tmp_creds.write_bytes(creds_bytes)
                os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(tmp_creds)

            if os.getenv("GOOGLE_APPLICATION_CREDENTIALS") and Path(
                os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
            ).exists():
                from googleapiclient.discovery import build

                self._sdk_service = build("drive", "v3", cache_discovery=False)
        except Exception as exc:
            print(f"[DriveClient] SDK init skipped, falling back to gdrive CLI: {exc}")
            self._sdk_service = None

    @property
    def has_gdrive_cli(self) -> bool:
        return shutil.which("gdrive") is not None

    def _list_folder_via_cli(self, folder_id: str) -> List[Dict[str, Any]]:
        cmd = ["gdrive", "readonly", "ls", folder_id, "--json", "--max", "500"]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            raise RuntimeError(f"gdrive ls failed: {res.stderr.strip()}")
        out = res.stdout.strip()
        if not out:
            return []
        data = json.loads(out)
        return data if isinstance(data, list) else []

    def _list_folder_via_sdk(self, folder_id: str) -> List[Dict[str, Any]]:
        query = f"'{folder_id}' in parents and trashed = false"
        fields = "files(id, name, mimeType, modifiedTime, size)"
        resp = (
            self._sdk_service.files()
            .list(q=query, fields=fields, pageSize=500)
            .execute()
        )
        return resp.get("files", [])

    def list_folder(self, folder_id: Optional[str] = None) -> List[Dict[str, Any]]:
        target_id = folder_id or self.parent_folder_id
        if self._sdk_service is not None:
            try:
                return self._list_folder_via_sdk(target_id)
            except Exception as exc:
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
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")

        if self._sdk_service is not None:
            try:
                from googleapiclient.http import MediaIoBaseDownload

                request = self._sdk_service.files().get_media(fileId=file_id)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
                tmp_path.write_bytes(fh.getvalue())
                tmp_path.replace(dest_path)
                return dest_path
            except Exception as exc:
                if not self.has_gdrive_cli:
                    raise exc

        if self.has_gdrive_cli:
            if tmp_path.exists():
                tmp_path.unlink()
            cmd = [
                "gdrive",
                "readonly",
                "download",
                file_id,
                "--out",
                str(tmp_path),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if res.returncode != 0:
                raise RuntimeError(
                    f"gdrive download failed for {file_id}: {res.stderr.strip()}"
                )
            tmp_path.replace(dest_path)
            return dest_path

        raise RuntimeError("No method available to download file from Google Drive.")
