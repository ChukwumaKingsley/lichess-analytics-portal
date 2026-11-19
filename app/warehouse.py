from collections import OrderedDict
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import logging
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Dict, Generator, List, Optional, Tuple

import numpy as np
import pandas as pd

from app import config
from app.drive_client import DriveClient

logger = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"^(__ALL__|[A-Za-z0-9_-]{2,30})$")
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _clean_num(val: Any, decimals: int = 1) -> Optional[float]:
    if val is None:
        return None
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return None
        return round(f, decimals)
    except Exception:
        return None


def _clean_int(val: Any, default: int = 0) -> int:
    if val is None:
        return default
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return default
        return int(round(f))
    except Exception:
        return default


def _sanitize_csv_cell(val: Any) -> Any:
    if isinstance(val, str) and val.startswith(FORMULA_PREFIXES):
        return f"'{val}"
    return val


class LichessWarehouse:
    """
    Automated SSIS-Replacement Warehouse & Real-Time Analytics Engine.
    Replaces:
      - Looping Through files.dtsx (multi-player Drive CSV ingestion -> unified lichess_games table)
      - Basic Transformations.dtsx (Multicast + Conditional Split into All / White / Black tables)
    """

    def __init__(self, drive_client: DriveClient):
        self.drive = drive_client
        self._lock = threading.Lock()
        self._discovery_lock = threading.Lock()
        self._player_locks: Dict[str, threading.Lock] = {}
        self._games_cache: "OrderedDict[str, pd.DataFrame]" = OrderedDict()
        self._ratings_cache: "OrderedDict[str, pd.DataFrame]" = OrderedDict()
        self._player_folders: Dict[str, Dict[str, Any]] = {}
        self._folders_cached_at: float = 0.0
        # Maps slice_cache_key (without page/page_size) -> (cached_at, base_payload, filtered_df)
        self._dashboard_cache: "OrderedDict[Tuple, Tuple[float, Dict[str, Any], pd.DataFrame]]" = (
            OrderedDict()
        )
        self._drive_calls_saved: int = 0
        self._drive_calls_made: int = 0
        self._init_db()

    @staticmethod
    def _validate_username(username: str, allow_all: bool = True) -> str:
        cleaned = (username or "").strip()
        if not allow_all and cleaned == "__ALL__":
            raise ValueError("Invalid Lichess username: '__ALL__' is not allowed here.")
        if not USERNAME_RE.fullmatch(cleaned):
            raise ValueError(
                f"Invalid Lichess username '{username}'. Use 2-30 letters, digits, '_', or '-'."
            )
        return cleaned

    def _get_player_lock(self, username: str) -> threading.Lock:
        key = username.lower()
        with self._lock:
            lock = self._player_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._player_locks[key] = lock
            return lock

    def _store_player_frames(
        self, username: str, df_games: pd.DataFrame, df_ratings: pd.DataFrame
    ) -> None:
        max_players = max(4, int(getattr(config, "MAX_CACHED_PLAYERS_IN_MEMORY", 16)))
        with self._lock:
            self._games_cache[username] = df_games
            self._games_cache.move_to_end(username)
            self._ratings_cache[username] = df_ratings
            self._ratings_cache.move_to_end(username)
            # Also invalidate any cached __ALL__ combined DataFrame
            self._games_cache.pop("__ALL__", None)
            self._ratings_cache.pop("__ALL__", None)

            while len(self._games_cache) > max_players:
                evicted_user, _ = self._games_cache.popitem(last=False)
                self._ratings_cache.pop(evicted_user, None)

    def _invalidate_dashboard_cache(self, username: Optional[str] = None) -> None:
        with self._lock:
            if username is None:
                self._dashboard_cache.clear()
            else:
                target = username.lower()
                keys_to_del = [
                    k
                    for k in self._dashboard_cache
                    if k[0].lower() in (target, "__all__")
                ]
                for k in keys_to_del:
                    del self._dashboard_cache[k]

    def get_cache_stats(self) -> Dict[str, Any]:
        with self._lock:
            mem_players = len(
                [k for k in self._games_cache if k != "__ALL__"]
            )
            return {
                "cached_players_in_memory": mem_players,
                "cached_dashboard_queries": len(self._dashboard_cache),
                "cached_drive_folders": len(self._player_folders),
                "drive_calls_made": self._drive_calls_made,
                "drive_calls_saved": self._drive_calls_saved,
                "folder_cache_ttl_s": config.DRIVE_FOLDER_CACHE_TTL,
                "player_sync_ttl_s": config.PLAYER_SYNC_TTL_SECONDS,
            }

    @contextmanager
    def _db_conn(self) -> Generator[sqlite3.Connection, None, None]:
        raw_conn = sqlite3.connect(
            str(config.WAREHOUSE_DB_PATH),
            timeout=15.0,
            check_same_thread=False,
        )
        raw_conn.row_factory = sqlite3.Row
        with closing(raw_conn) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            yield conn

    def _init_db(self) -> None:
        with self._db_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_metadata (
                    username TEXT PRIMARY KEY,
                    folder_id TEXT,
                    games_file_id TEXT,
                    games_modified_time TEXT,
                    games_size INTEGER,
                    rating_file_id TEXT,
                    rating_modified_time TEXT,
                    total_games INTEGER DEFAULT 0,
                    white_games INTEGER DEFAULT 0,
                    black_games INTEGER DEFAULT 0,
                    rating_days INTEGER DEFAULT 0,
                    last_synced_at TEXT
                )
                """
            )
            conn.commit()

    def discover_players(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        Discover all player folders in Google Drive (with thread-safe TTL cache and cold-start deduplication)
        and merge with warehouse sync status.
        """
        with self._discovery_lock:
            now = time.time()
            with self._lock:
                cache_expired = (
                    now - self._folders_cached_at
                ) > config.DRIVE_FOLDER_CACHE_TTL
                need_fetch = force_refresh or not self._player_folders or cache_expired

            if need_fetch:
                try:
                    folders = self.drive.list_player_folders()
                    valid_folders = {
                        f["name"]: f
                        for f in folders
                        if f.get("name") and USERNAME_RE.fullmatch(f["name"])
                    }
                    with self._lock:
                        self._player_folders = valid_folders
                        self._folders_cached_at = time.time()
                        self._drive_calls_made += 1
                except Exception as exc:
                    logger.warning("[Warehouse] Warning listing Drive folders: %s", exc)
            else:
                with self._lock:
                    self._drive_calls_saved += 1

        with self._lock:
            folders_snapshot = dict(self._player_folders)

        with self._db_conn() as conn:
            rows = conn.execute("SELECT * FROM sync_metadata").fetchall()
            meta_map = {
                r["username"]: dict(r)
                for r in rows
                if r["username"] and USERNAME_RE.fullmatch(r["username"])
            }

        # Only include locally cached directories that actually contain a games CSV
        local_players = set()
        if config.DATA_CACHE_DIR.exists():
            for child in config.DATA_CACHE_DIR.iterdir():
                if (
                    child.is_dir()
                    and not child.name.startswith(".")
                    and USERNAME_RE.fullmatch(child.name)
                    and (child / f"games_{child.name}.csv").exists()
                ):
                    local_players.add(child.name)

        all_usernames = sorted(
            set(folders_snapshot.keys())
            | {u for u, m in meta_map.items() if m.get("total_games", 0) > 0}
            | local_players,
            key=lambda s: s.lower(),
        )

        players_list = []
        for uname in all_usernames:
            f_info = folders_snapshot.get(uname, {})
            m_info = meta_map.get(uname, {})
            local_csv = config.DATA_CACHE_DIR / uname / f"games_{uname}.csv"
            is_cached = local_csv.exists()
            players_list.append(
                {
                    "username": uname,
                    "folder_id": f_info.get("id") or m_info.get("folder_id"),
                    "folder_modified_time": f_info.get("modifiedTime"),
                    "is_synced": is_cached and m_info.get("total_games", 0) > 0,
                    "total_games": m_info.get("total_games", 0),
                    "white_games": m_info.get("white_games", 0),
                    "black_games": m_info.get("black_games", 0),
                    "rating_days": m_info.get("rating_days", 0),
                    "games_modified_time": m_info.get("games_modified_time"),
                    "last_synced_at": m_info.get("last_synced_at"),
                }
            )
        return players_list

    def sync_player(self, username: str, force: bool = False) -> Dict[str, Any]:
        """
        Sync a player's CSV files from Google Drive into local cache and SQLite warehouse.
        Skips Drive metadata/download calls when local CSV is fresh within PLAYER_SYNC_TTL_SECONDS
        unless force=True. Thread-safe per player (CONC-02) and safe against path traversal (SEC-02).
        """
        username = self._validate_username(username, allow_all=False)

        with self._lock:
            has_folders = bool(self._player_folders)
        if not has_folders:
            self.discover_players(force_refresh=False)

        with self._lock:
            matched_name = username
            for k in self._player_folders:
                if k.lower() == username.lower():
                    matched_name = k
                    break
            username = matched_name
            known_in_drive = username in self._player_folders

        if not known_in_drive and force:
            self.discover_players(force_refresh=True)
            with self._lock:
                for k in self._player_folders:
                    if k.lower() == username.lower():
                        username = k
                        break

        player_lock = self._get_player_lock(username)
        with player_lock:
            with self._lock:
                folder_info = self._player_folders.get(username)

            base_cache_dir = config.DATA_CACHE_DIR.resolve()
            player_dir = (config.DATA_CACHE_DIR / username).resolve()
            if not player_dir.is_relative_to(base_cache_dir):
                raise ValueError(f"Path traversal blocked for username '{username}'.")

            games_csv_path = player_dir / f"games_{username}.csv"
            rating_csv_path = player_dir / f"rating_history_{username}.csv"

            # SEC-02 fix: if player does not exist in Drive and has no local CSV, do not create directories or DB rows
            if not folder_info and not games_csv_path.exists():
                return {
                    "username": username,
                    "total_games": 0,
                    "white_games": 0,
                    "black_games": 0,
                    "rating_days": 0,
                    "games_modified_time": None,
                    "last_synced_at": None,
                }

            player_dir.mkdir(parents=True, exist_ok=True)

            with self._db_conn() as conn:
                row = conn.execute(
                    "SELECT * FROM sync_metadata WHERE username = ?", (username,)
                ).fetchone()
                existing_meta = dict(row) if row else {}

            local_is_fresh = False
            if (
                not force
                and games_csv_path.exists()
                and existing_meta.get("last_synced_at")
            ):
                try:
                    last_dt = datetime.fromisoformat(existing_meta["last_synced_at"])
                    age_s = (datetime.now(timezone.utc) - last_dt).total_seconds()
                    if age_s < config.PLAYER_SYNC_TTL_SECONDS:
                        local_is_fresh = True
                except Exception:
                    pass

            games_file_meta: Dict[str, Any] = {}
            rating_file_meta: Dict[str, Any] = {}

            if local_is_fresh:
                with self._lock:
                    self._drive_calls_saved += 1
            elif folder_info and folder_info.get("id"):
                folder_id = folder_info["id"]
                files_map = self.drive.get_player_files_metadata(folder_id)
                with self._lock:
                    self._drive_calls_made += 1
                games_file_meta = files_map.get(f"games_{username}.csv", {})
                rating_file_meta = files_map.get(f"rating_history_{username}.csv", {})

                if games_file_meta.get("id"):
                    need_games_dl = (
                        not games_csv_path.exists()
                        or games_file_meta.get("modifiedTime")
                        != existing_meta.get("games_modified_time")
                    )
                    if need_games_dl:
                        self.drive.download_file(
                            games_file_meta["id"], games_csv_path
                        )
                        with self._lock:
                            self._drive_calls_made += 1
                    else:
                        with self._lock:
                            self._drive_calls_saved += 1

                if rating_file_meta.get("id"):
                    need_rating_dl = (
                        not rating_csv_path.exists()
                        or rating_file_meta.get("modifiedTime")
                        != existing_meta.get("rating_modified_time")
                    )
                    if need_rating_dl:
                        self.drive.download_file(
                            rating_file_meta["id"], rating_csv_path
                        )
                        with self._lock:
                            self._drive_calls_made += 1
                    else:
                        with self._lock:
                            self._drive_calls_saved += 1

            df_games = self._load_and_normalize_games(username, games_csv_path)
            df_ratings = self._load_and_normalize_ratings(username, rating_csv_path)

            self._store_player_frames(username, df_games, df_ratings)
            self._invalidate_dashboard_cache(username)

            total_games = len(df_games)
            white_games = (
                int((df_games["played_as"] == "white").sum())
                if total_games > 0
                else 0
            )
            black_games = (
                int((df_games["played_as"] == "black").sum())
                if total_games > 0
                else 0
            )
            rating_days = len(df_ratings)
            now_iso = datetime.now(timezone.utc).isoformat()

            with self._db_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO sync_metadata (
                        username, folder_id, games_file_id, games_modified_time, games_size,
                        rating_file_id, rating_modified_time, total_games, white_games,
                        black_games, rating_days, last_synced_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(username) DO UPDATE SET
                        folder_id = excluded.folder_id,
                        games_file_id = excluded.games_file_id,
                        games_modified_time = excluded.games_modified_time,
                        games_size = excluded.games_size,
                        rating_file_id = excluded.rating_file_id,
                        rating_modified_time = excluded.rating_modified_time,
                        total_games = excluded.total_games,
                        white_games = excluded.white_games,
                        black_games = excluded.black_games,
                        rating_days = excluded.rating_days,
                        last_synced_at = excluded.last_synced_at
                    """,
                    (
                        username,
                        folder_info.get("id")
                        if folder_info
                        else existing_meta.get("folder_id"),
                        games_file_meta.get("id", existing_meta.get("games_file_id")),
                        games_file_meta.get(
                            "modifiedTime", existing_meta.get("games_modified_time")
                        ),
                        int(games_file_meta.get("size", 0) or 0),
                        rating_file_meta.get(
                            "id", existing_meta.get("rating_file_id")
                        ),
                        rating_file_meta.get(
                            "modifiedTime", existing_meta.get("rating_modified_time")
                        ),
                        total_games,
                        white_games,
                        black_games,
                        rating_days,
                        now_iso,
                    ),
                )
                conn.commit()

            return {
                "username": username,
                "total_games": total_games,
                "white_games": white_games,
                "black_games": black_games,
                "rating_days": rating_days,
                "games_modified_time": games_file_meta.get(
                    "modifiedTime", existing_meta.get("games_modified_time")
                ),
                "last_synced_at": now_iso,
            }

    def _load_and_normalize_games(self, username: str, csv_path: Path) -> pd.DataFrame:
        if not csv_path.exists():
            return pd.DataFrame()
        df = pd.read_csv(csv_path, encoding="utf-8-sig", low_memory=False)
        if df.empty:
            return df

        if "game_id" in df.columns:
            df = df.drop_duplicates(subset=["game_id"], keep="first")

        for col in (
            "played_as",
            "opponent_color",
            "result",
            "speed",
            "status",
            "source",
        ):
            if col in df.columns:
                df[col] = df[col].astype(str).str.strip().str.lower()

        if "player_name" not in df.columns:
            df["player_name"] = username
        else:
            df["player_name"] = df["player_name"].fillna(username)

        for bcol in ("rated", "tournament"):
            if bcol in df.columns:
                df[bcol] = (
                    df[bcol]
                    .astype(str)
                    .str.strip()
                    .str.lower()
                    .isin(["true", "1", "yes"])
                )
            else:
                df[bcol] = False

        # Repair legacy CSV rows affected by historical BUG-01 where non-tournament sources were marked tournament=True
        if "source" in df.columns:
            non_tourney_sources = df["source"].isin(
                ["pool", "lobby", "friend", "ai", "import"]
            )
            df["tournament"] = df["tournament"] & ~non_tourney_sources

        for ncol in (
            "player_rating",
            "player_rating_diff",
            "opponent_rating",
            "opponent_rating_diff",
            "opening_ply",
            "move_count",
            "turns",
        ):
            if ncol in df.columns:
                df[ncol] = pd.to_numeric(df[ncol], errors="coerce")
            else:
                df[ncol] = np.nan

        df["created_dt"] = pd.to_datetime(df["created_at"], errors="coerce", utc=False)
        df = df.dropna(subset=["created_dt"]).sort_values(
            by="created_dt", ascending=False
        )
        df["date_str"] = df["created_dt"].dt.strftime("%Y-%m-%d")
        df["month_str"] = df["created_dt"].dt.strftime("%Y-%m")
        df["dow"] = df["created_dt"].dt.day_of_week  # 0=Mon .. 6=Sun
        df["hour"] = df["created_dt"].dt.hour

        df["opening_eco"] = df.get("opening_eco", pd.Series(dtype=str)).fillna("N/A")
        df["opening_name"] = df.get("opening_name", pd.Series(dtype=str)).fillna(
            "Unknown Opening"
        )
        df["opponent_name"] = df.get("opponent_name", pd.Series(dtype=str)).fillna(
            "Anonymous"
        )
        df["time_control"] = df.get("time_control", pd.Series(dtype=str)).fillna("N/A")

        return df.reset_index(drop=True)

    def _load_and_normalize_ratings(
        self, username: str, csv_path: Path
    ) -> pd.DataFrame:
        if not csv_path.exists():
            return pd.DataFrame()
        df = pd.read_csv(csv_path, encoding="utf-8-sig", low_memory=False)
        if df.empty or "date" not in df.columns:
            return pd.DataFrame()
        df["date_dt"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date_dt"]).sort_values(by="date_dt", ascending=True)
        df["date_str"] = df["date_dt"].dt.strftime("%Y-%m-%d")
        for col in df.columns:
            if col not in ("date", "date_dt", "date_str"):
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df.reset_index(drop=True)

    def ensure_player_loaded(
        self, username: str, force_sync: bool = False
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        username = self._validate_username(username, allow_all=True)
        if username == "__ALL__":
            return self._load_all_synced_players(force_sync=force_sync)

        # Match canonical casing if already in cache or Drive folders
        with self._lock:
            for k in list(self._games_cache.keys()) + list(self._player_folders.keys()):
                if k.lower() == username.lower():
                    username = k
                    break
            has_cached = username in self._games_cache and not force_sync
            if has_cached:
                self._games_cache.move_to_end(username)
                if username in self._ratings_cache:
                    self._ratings_cache.move_to_end(username)
                return (
                    self._games_cache.get(username, pd.DataFrame()),
                    self._ratings_cache.get(username, pd.DataFrame()),
                )

        self.sync_player(username, force=force_sync)

        with self._lock:
            return (
                self._games_cache.get(username, pd.DataFrame()),
                self._ratings_cache.get(username, pd.DataFrame()),
            )

    def _load_all_synced_players(
        self, force_sync: bool = False
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Combines all synced players into a unified DataFrame (SSIS Looping Through files.dtsx).
        Caches the combined '__ALL__' DataFrame until any individual player is synced (PERF-02).
        """
        if not force_sync:
            with self._lock:
                if "__ALL__" in self._games_cache:
                    return (
                        self._games_cache["__ALL__"],
                        self._ratings_cache.get("__ALL__", pd.DataFrame()),
                    )

        players = self.discover_players(force_refresh=force_sync)
        dfs = []
        for p in players:
            uname = p["username"]
            local_csv = config.DATA_CACHE_DIR / uname / f"games_{uname}.csv"
            with self._lock:
                in_mem = uname in self._games_cache
            if local_csv.exists() or force_sync:
                if not in_mem or force_sync:
                    self.sync_player(uname, force=force_sync)
                with self._lock:
                    gdf = self._games_cache.get(uname)
                if gdf is not None and not gdf.empty:
                    dfs.append(gdf)

        # BUG-06 fix: if fewer than 2 players were synced on disk (e.g. cold container restart),
        # automatically sync available Drive player folders so __ALL__ represents the multi-player warehouse.
        if len(dfs) < min(2, len(players)):
            dfs = []
            for p in players:
                uname = p["username"]
                self.sync_player(uname, force=False)
                with self._lock:
                    gdf = self._games_cache.get(uname)
                if gdf is not None and not gdf.empty:
                    dfs.append(gdf)

        if not dfs:
            return pd.DataFrame(), pd.DataFrame()

        combined = (
            pd.concat(dfs, ignore_index=True)
            .drop_duplicates(subset=["game_id", "player_name"], keep="first")
            .sort_values(by="created_dt", ascending=False)
            .reset_index(drop=True)
        )
        with self._lock:
            self._games_cache["__ALL__"] = combined
            self._ratings_cache["__ALL__"] = pd.DataFrame()
        return combined, pd.DataFrame()

    @staticmethod
    def _compute_slice_stats(df: pd.DataFrame) -> Dict[str, Any]:
        total = len(df)
        if total == 0:
            return {
                "games": 0,
                "wins": 0,
                "draws": 0,
                "losses": 0,
                "win_rate": 0.0,
                "draw_rate": 0.0,
                "loss_rate": 0.0,
                "avg_rating": None,
                "peak_rating": None,
                "net_rating_diff": 0.0,
                "avg_opponent_rating": None,
                "avg_turns": None,
                "top_opening": "N/A",
            }
        wins = int((df["result"] == "win").sum())
        draws = int((df["result"] == "draw").sum())
        losses = int((df["result"] == "lose").sum())
        top_op = "N/A"
        valid_ops = df[df["opening_name"] != "Unknown Opening"]["opening_name"]
        if not valid_ops.empty:
            top_op = str(valid_ops.mode().iloc[0])

        return {
            "games": total,
            "wins": wins,
            "draws": draws,
            "losses": losses,
            "win_rate": round(wins * 100.0 / total, 1),
            "draw_rate": round(draws * 100.0 / total, 1),
            "loss_rate": round(losses * 100.0 / total, 1),
            "avg_rating": _clean_num(df["player_rating"].mean(), 0),
            "peak_rating": _clean_num(df["player_rating"].max(), 0),
            "net_rating_diff": _clean_num(df["player_rating_diff"].sum(), 0) or 0.0,
            "avg_opponent_rating": _clean_num(df["opponent_rating"].mean(), 0),
            "avg_turns": _clean_num(df["turns"].mean(), 1),
            "top_opening": top_op,
        }

    @staticmethod
    def _paginate_games(
        df: pd.DataFrame, username: str, page: int, page_size: int
    ) -> Dict[str, Any]:
        """
        Fast O(page_size) pagination using to_dict(orient='records') instead of iterrows() (PERF-01).
        """
        total = len(df)
        page = max(1, page)
        page_size = max(1, min(100, page_size))
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        page_df = df.iloc[start_idx:end_idx]

        games_list = []
        for row in page_df.to_dict(orient="records"):
            gid = str(row.get("game_id", ""))
            games_list.append(
                {
                    "game_id": gid,
                    "lichess_url": f"https://lichess.org/{gid}",
                    "created_at": str(row.get("created_at", ""))[:19],
                    "speed": str(row.get("speed", "")),
                    "time_control": str(row.get("time_control", "")),
                    "rated": bool(row.get("rated", False)),
                    "source": str(row.get("source", "")),
                    "player_name": str(row.get("player_name", username)),
                    "played_as": str(row.get("played_as", "")),
                    "player_rating": _clean_int(row.get("player_rating"), 0) or None,
                    "player_rating_diff": _clean_int(row.get("player_rating_diff"), 0),
                    "opponent_name": str(row.get("opponent_name", "")),
                    "opponent_rating": _clean_int(row.get("opponent_rating"), 0)
                    or None,
                    "result": str(row.get("result", "")),
                    "status": str(row.get("status", "")),
                    "opening_eco": str(row.get("opening_eco", "")),
                    "opening_name": str(row.get("opening_name", "")),
                    "turns": _clean_int(row.get("turns"), 0) or None,
                    "move_count": _clean_int(row.get("move_count"), 0) or None,
                }
            )

        return {
            "page": page,
            "page_size": page_size,
            "total_filtered": total,
            "total_pages": max(1, math.ceil(total / page_size)),
            "items": games_list,
        }

    def get_dashboard_data(
        self,
        username: str,
        color: str = "all",
        speed: str = "all",
        rated: str = "all",
        source: str = "all",
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        search: Optional[str] = None,
        page: int = 1,
        page_size: int = 25,
        force_sync: bool = False,
    ) -> Dict[str, Any]:
        username = self._validate_username(username, allow_all=True)

        # PERF-01 fix: cache key is independent of (page, page_size) so pagination reuses cached analytics
        slice_cache_key = (
            username.lower(),
            color.lower(),
            speed.lower(),
            rated.lower(),
            source.lower(),
            date_from or "",
            date_to or "",
            (search or "").strip().lower(),
        )
        if not force_sync:
            with self._lock:
                cached_entry = self._dashboard_cache.get(slice_cache_key)
                if (
                    cached_entry
                    and (time.time() - cached_entry[0])
                    < config.DASHBOARD_CACHE_TTL_SECONDS
                ):
                    self._dashboard_cache.move_to_end(slice_cache_key)
                    cached_ts, base_payload, cached_df = cached_entry
                    return {
                        **base_payload,
                        "recent_games": self._paginate_games(
                            cached_df, base_payload["username"], page, page_size
                        ),
                        "cache_info": {
                            "source": "memory_cache",
                            "cached_at": cached_ts,
                        },
                    }

        df_raw, df_ratings = self.ensure_player_loaded(username, force_sync=force_sync)

        if df_raw.empty:
            return {
                "username": username,
                "empty": True,
                "summary": self._compute_slice_stats(df_raw),
                "cache_info": {"source": "empty"},
            }

        df_base = df_raw

        if speed and speed.lower() != "all":
            speeds = [s.strip().lower() for s in speed.split(",") if s.strip()]
            df_base = df_base[df_base["speed"].isin(speeds)]

        if rated and rated.lower() in ("true", "rated"):
            df_base = df_base[df_base["rated"] == True]  # noqa: E712
        elif rated and rated.lower() in ("false", "casual"):
            df_base = df_base[df_base["rated"] == False]  # noqa: E712

        if source and source.lower() != "all":
            df_base = df_base[df_base["source"] == source.lower()]

        if date_from:
            df_base = df_base[df_base["date_str"] >= date_from]
        if date_to:
            df_base = df_base[df_base["date_str"] <= date_to]

        if search and search.strip():
            q = search.strip().lower()
            mask = (
                df_base["opening_name"].str.lower().str.contains(q, na=False)
                | df_base["opening_eco"].str.lower().str.contains(q, na=False)
                | df_base["opponent_name"].str.lower().str.contains(q, na=False)
                | df_base["game_id"].str.lower().str.contains(q, na=False)
            )
            df_base = df_base[mask]

        # SSIS Conditional Split stats (White vs Black) on df_base
        df_white = df_base[df_base["played_as"] == "white"]
        df_black = df_base[df_base["played_as"] == "black"]
        color_split = {
            "white": self._compute_slice_stats(df_white),
            "black": self._compute_slice_stats(df_black),
        }

        if color and color.lower() in ("white", "black"):
            df = df_base[df_base["played_as"] == color.lower()]
        else:
            df = df_base

        total = len(df)
        base_stats = self._compute_slice_stats(df)

        # BUG-03 fix: accurate current_rating and peak_rating when speed="all" or username="__ALL__"
        current_rating = None
        peak_rating = None
        peak_date = None
        df_with_rating = df.dropna(subset=["player_rating"])

        if not df_with_rating.empty:
            idx_max = df_with_rating["player_rating"].idxmax()
            peak_rating = _clean_int(df_with_rating.loc[idx_max, "player_rating"])
            raw_peak_date = str(df_with_rating.loc[idx_max, "date_str"])
            if username == "__ALL__":
                peak_player = str(df_with_rating.loc[idx_max, "player_name"])
                peak_date = f"{raw_peak_date} ({peak_player})"
                # Average of each active player's latest rating in the filtered slice
                latest_per_player = (
                    df_with_rating.groupby("player_name")["player_rating"].first()
                )
                current_rating = _clean_int(latest_per_player.mean())
            else:
                peak_date = raw_peak_date
                if speed.lower() == "all":
                    # Use the player's most-played speed in the slice so casual side-variants don't distort Current Rating
                    top_speed_mode = df_with_rating["speed"].mode()
                    if not top_speed_mode.empty:
                        primary_speed = top_speed_mode.iloc[0]
                        primary_ratings = df_with_rating[
                            df_with_rating["speed"] == primary_speed
                        ]["player_rating"]
                        current_rating = _clean_int(primary_ratings.iloc[0])
                    else:
                        current_rating = _clean_int(
                            df_with_rating["player_rating"].iloc[0]
                        )
                else:
                    current_rating = _clean_int(
                        df_with_rating["player_rating"].iloc[0]
                    )

        summary = {
            **base_stats,
            "unfiltered_total_games": len(df_raw),
            "white_games": len(df_white),
            "black_games": len(df_black),
            "rated_games": int(df["rated"].sum()) if total > 0 else 0,
            "tournament_games": int(df["tournament"].sum()) if total > 0 else 0,
            "current_rating": current_rating,
            "peak_rating": peak_rating,
            "peak_rating_date": peak_date,
            "avg_moves": _clean_num(df["move_count"].mean(), 1) if total > 0 else None,
            "first_game_date": str(df["date_str"].min()) if total > 0 else None,
            "last_game_date": str(df["date_str"].max()) if total > 0 else None,
        }

        # 1. Speed Breakdown
        speed_breakdown = []
        if total > 0:
            for spd, grp in df.groupby("speed"):
                st = self._compute_slice_stats(grp)
                vr = grp["player_rating"].dropna()
                cur_r = _clean_int(vr.iloc[0]) if not vr.empty else None
                speed_breakdown.append(
                    {
                        "speed": str(spd),
                        **st,
                        "current_rating": cur_r,
                    }
                )
            speed_breakdown.sort(key=lambda x: x["games"], reverse=True)

        # 2. Termination / Status Breakdown
        status_breakdown = []
        if total > 0:
            for status_val, grp in df.groupby("status"):
                cnt = len(grp)
                w_cnt = int((grp["result"] == "win").sum())
                d_cnt = int((grp["result"] == "draw").sum())
                l_cnt = int((grp["result"] == "lose").sum())
                status_breakdown.append(
                    {
                        "status": str(status_val),
                        "count": cnt,
                        "share": round(cnt * 100.0 / total, 1),
                        "wins": w_cnt,
                        "draws": d_cnt,
                        "losses": l_cnt,
                    }
                )
            status_breakdown.sort(key=lambda x: x["count"], reverse=True)

        # 3. Time Controls Breakdown (top 10)
        time_controls = []
        if total > 0:
            for tc, grp in df.groupby("time_control"):
                st = self._compute_slice_stats(grp)
                time_controls.append({"time_control": str(tc), **st})
            time_controls.sort(key=lambda x: x["games"], reverse=True)
            time_controls = time_controls[:10]

        # 4. Openings Explorer (Overall + White + Black)
        def _build_openings(
            sub_df: pd.DataFrame, limit: int = 12
        ) -> List[Dict[str, Any]]:
            if sub_df.empty:
                return []
            valid = sub_df[sub_df["opening_name"] != "Unknown Opening"]
            if valid.empty:
                return []
            items = []
            for (eco, name), grp in valid.groupby(["opening_eco", "opening_name"]):
                cnt = len(grp)
                w = int((grp["result"] == "win").sum())
                d = int((grp["result"] == "draw").sum())
                l = int((grp["result"] == "lose").sum())
                items.append(
                    {
                        "eco": str(eco),
                        "name": str(name),
                        "short_name": str(name).split(":")[0].strip(),
                        "games": cnt,
                        "wins": w,
                        "draws": d,
                        "losses": l,
                        "win_rate": round(w * 100.0 / cnt, 1),
                        "draw_rate": round(d * 100.0 / cnt, 1),
                        "loss_rate": round(l * 100.0 / cnt, 1),
                        "net_rating_diff": _clean_num(
                            grp["player_rating_diff"].sum(), 0
                        )
                        or 0.0,
                        "avg_turns": _clean_num(grp["turns"].mean(), 1),
                    }
                )
            items.sort(key=lambda x: x["games"], reverse=True)
            return items[:limit]

        openings = {
            "overall": _build_openings(df, 15),
            "white": _build_openings(df_white, 10),
            "black": _build_openings(df_black, 10),
        }

        # 5. Monthly Volume & Performance Trends
        monthly_trends = []
        if total > 0:
            for m_str, grp in df.groupby("month_str"):
                cnt = len(grp)
                w = int((grp["result"] == "win").sum())
                d = int((grp["result"] == "draw").sum())
                l = int((grp["result"] == "lose").sum())
                monthly_trends.append(
                    {
                        "month": str(m_str),
                        "games": cnt,
                        "wins": w,
                        "draws": d,
                        "losses": l,
                        "win_rate": round(w * 100.0 / cnt, 1),
                        "avg_rating": _clean_num(grp["player_rating"].mean(), 0),
                        "net_rating_diff": _clean_num(
                            grp["player_rating_diff"].sum(), 0
                        )
                        or 0.0,
                    }
                )
            monthly_trends.sort(key=lambda x: x["month"])

        # 6. Vectorized Day-of-Week & Hour-of-Day Performance (PERF-03 fix)
        dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        dow_stats = []
        hour_stats = []
        if total > 0:
            win_mask = (df["result"] == "win").astype(int)
            dow_total = df["dow"].value_counts().to_dict()
            dow_wins = win_mask.groupby(df["dow"]).sum().to_dict()
            for d_idx in range(7):
                cnt = int(dow_total.get(d_idx, 0))
                w = int(dow_wins.get(d_idx, 0))
                dow_stats.append(
                    {
                        "day": dow_names[d_idx],
                        "games": cnt,
                        "win_rate": round(w * 100.0 / cnt, 1) if cnt > 0 else 0.0,
                    }
                )

            hour_total = df["hour"].value_counts().to_dict()
            hour_wins = win_mask.groupby(df["hour"]).sum().to_dict()
            for h_idx in range(24):
                cnt = int(hour_total.get(h_idx, 0))
                w = int(hour_wins.get(h_idx, 0))
                hour_stats.append(
                    {
                        "hour": f"{h_idx:02d}:00",
                        "games": cnt,
                        "win_rate": round(w * 100.0 / cnt, 1) if cnt > 0 else 0.0,
                    }
                )

        # 7. Frequent Opponents / Head-to-Head Rivalries
        opponents = []
        if total > 0:
            for opp, grp in df.groupby("opponent_name"):
                if opp in ("Anonymous", "Unnamed"):
                    continue
                cnt = len(grp)
                w = int((grp["result"] == "win").sum())
                d = int((grp["result"] == "draw").sum())
                l = int((grp["result"] == "lose").sum())
                opponents.append(
                    {
                        "opponent": str(opp),
                        "games": cnt,
                        "wins": w,
                        "draws": d,
                        "losses": l,
                        "win_rate": round(w * 100.0 / cnt, 1),
                        "avg_opponent_rating": _clean_num(
                            grp["opponent_rating"].mean(), 0
                        ),
                        "net_rating_diff": _clean_num(
                            grp["player_rating_diff"].sum(), 0
                        )
                        or 0.0,
                    }
                )
            opponents.sort(key=lambda x: x["games"], reverse=True)
            opponents = opponents[:12]

        # 8. Rating History Time Series
        rating_history_payload = self._build_rating_history_payload(
            df_ratings, df_raw, date_from=date_from, date_to=date_to
        )

        base_payload = {
            "username": username,
            "empty": False,
            "filters_applied": {
                "color": color,
                "speed": speed,
                "rated": rated,
                "source": source,
                "date_from": date_from,
                "date_to": date_to,
                "search": search,
            },
            "summary": summary,
            "color_split": color_split,
            "speed_breakdown": speed_breakdown,
            "status_breakdown": status_breakdown,
            "time_controls": time_controls,
            "openings": openings,
            "monthly_trends": monthly_trends,
            "activity": {
                "by_day_of_week": dow_stats,
                "by_hour": hour_stats,
            },
            "opponents": opponents,
            "rating_history": rating_history_payload,
        }

        now_ts = time.time()
        with self._lock:
            self._dashboard_cache[slice_cache_key] = (now_ts, base_payload, df)
            self._dashboard_cache.move_to_end(slice_cache_key)
            while len(self._dashboard_cache) > 128:
                self._dashboard_cache.popitem(last=False)

        return {
            **base_payload,
            "recent_games": self._paginate_games(df, username, page, page_size),
            "cache_info": {
                "source": "drive_sync" if force_sync else "warehouse_compute",
                "cached_at": now_ts,
            },
        }

    def _build_rating_history_payload(
        self,
        df_ratings: pd.DataFrame,
        df_games: pd.DataFrame,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not df_ratings.empty:
            r_df = df_ratings
            if date_from:
                r_df = r_df[r_df["date_str"] >= date_from]
            if date_to:
                r_df = r_df[r_df["date_str"] <= date_to]

            if not r_df.empty:
                step = max(1, len(r_df) // 180)
                sampled = r_df.iloc[::step].copy()
                if r_df.index[-1] not in sampled.index:
                    sampled = pd.concat([sampled, r_df.iloc[[-1]]])

                categories = [
                    c
                    for c in sampled.columns
                    if c not in ("date", "date_dt", "date_str")
                ]
                series = {}
                latest_ratings = {}
                for cat in categories:
                    col_vals = sampled[cat]
                    if col_vals.notna().any():
                        series[cat] = [
                            _clean_int(v) if pd.notna(v) else None for v in col_vals
                        ]
                        non_null = r_df[cat].dropna()
                        if not non_null.empty:
                            latest_ratings[cat] = {
                                "current": _clean_int(non_null.iloc[-1]),
                                "peak": _clean_int(non_null.max()),
                                "min": _clean_int(non_null.min()),
                            }

                return {
                    "source": "rating_history_csv",
                    "dates": list(sampled["date_str"]),
                    "series": series,
                    "latest_by_category": latest_ratings,
                }

        if df_games.empty:
            return {
                "source": "none",
                "dates": [],
                "series": {},
                "latest_by_category": {},
            }

        g_df = df_games.dropna(subset=["player_rating"]).copy()
        if date_from:
            g_df = g_df[g_df["date_str"] >= date_from]
        if date_to:
            g_df = g_df[g_df["date_str"] <= date_to]
        if g_df.empty:
            return {
                "source": "none",
                "dates": [],
                "series": {},
                "latest_by_category": {},
            }

        piv = (
            g_df.groupby(["month_str", "speed"])["player_rating"]
            .mean()
            .unstack("speed")
            .sort_index()
            .ffill()
        )
        series = {}
        latest = {}
        for col in piv.columns:
            series[str(col)] = [
                _clean_int(v) if pd.notna(v) else None for v in piv[col]
            ]
            nn = piv[col].dropna()
            if not nn.empty:
                latest[str(col)] = {
                    "current": _clean_int(nn.iloc[-1]),
                    "peak": _clean_int(nn.max()),
                    "min": _clean_int(nn.min()),
                }

        return {
            "source": "games_monthly_avg",
            "dates": [str(x) for x in piv.index],
            "series": series,
            "latest_by_category": latest,
        }

    def export_filtered_csv(
        self,
        username: str,
        color: str = "all",
        speed: str = "all",
        rated: str = "all",
        source: str = "all",
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        search: Optional[str] = None,
    ) -> str:
        """
        Export filtered dataset as CSV (replacing SSIS Flat File / Excel Destination),
        with formula injection neutralization on text columns (SEC-05).
        """
        username = self._validate_username(username, allow_all=True)
        df_raw, _ = self.ensure_player_loaded(username, force_sync=False)
        if df_raw.empty:
            return ""
        df = df_raw
        if color and color.lower() in ("white", "black"):
            df = df[df["played_as"] == color.lower()]
        if speed and speed.lower() != "all":
            speeds = [s.strip().lower() for s in speed.split(",") if s.strip()]
            df = df[df["speed"].isin(speeds)]
        if rated and rated.lower() in ("true", "rated"):
            df = df[df["rated"] == True]  # noqa: E712
        elif rated and rated.lower() in ("false", "casual"):
            df = df[df["rated"] == False]  # noqa: E712
        if source and source.lower() != "all":
            df = df[df["source"] == source.lower()]
        if date_from:
            df = df[df["date_str"] >= date_from]
        if date_to:
            df = df[df["date_str"] <= date_to]
        if search and search.strip():
            q = search.strip().lower()
            mask = (
                df["opening_name"].str.lower().str.contains(q, na=False)
                | df["opening_eco"].str.lower().str.contains(q, na=False)
                | df["opponent_name"].str.lower().str.contains(q, na=False)
                | df["game_id"].str.lower().str.contains(q, na=False)
            )
            df = df[mask]

        export_cols = [
            c
            for c in (
                "game_id",
                "rated",
                "speed",
                "created_at",
                "last_move_at",
                "status",
                "source",
                "player_name",
                "played_as",
                "opponent_name",
                "opponent_color",
                "player_rating",
                "player_rating_diff",
                "opponent_rating",
                "opponent_rating_diff",
                "result",
                "opening_eco",
                "opening_name",
                "opening_ply",
                "tournament",
                "time_control",
                "move_count",
                "turns",
            )
            if c in df.columns
        ]
        out_df = df[export_cols].copy()
        for scol in (
            "game_id",
            "player_name",
            "opponent_name",
            "opening_eco",
            "opening_name",
            "time_control",
            "status",
            "source",
        ):
            if scol in out_df.columns:
                out_df[scol] = out_df[scol].map(_sanitize_csv_cell)
        return out_df.to_csv(index=False)
