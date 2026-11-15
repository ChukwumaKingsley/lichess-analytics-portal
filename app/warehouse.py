import math
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from app.config import DATA_CACHE_DIR, WAREHOUSE_DB_PATH
from app.drive_client import DriveClient


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
        self._games_cache: Dict[str, pd.DataFrame] = {}
        self._ratings_cache: Dict[str, pd.DataFrame] = {}
        self._player_folders: Dict[str, Dict[str, Any]] = {}
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(WAREHOUSE_DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_conn() as conn:
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
        """Discover all player folders in Google Drive and merge with warehouse sync status."""
        if force_refresh or not self._player_folders:
            try:
                folders = self.drive.list_player_folders()
                self._player_folders = {f["name"]: f for f in folders}
            except Exception as exc:
                print(f"[Warehouse] Warning listing Drive folders: {exc}")

        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM sync_metadata").fetchall()
            meta_map = {r["username"]: dict(r) for r in rows}

        # Also check any locally cached player directories
        local_players = set()
        if DATA_CACHE_DIR.exists():
            for child in DATA_CACHE_DIR.iterdir():
                if child.is_dir() and not child.name.startswith("."):
                    local_players.add(child.name)

        all_usernames = sorted(
            set(self._player_folders.keys()) | set(meta_map.keys()) | local_players,
            key=lambda s: s.lower(),
        )

        players_list = []
        for uname in all_usernames:
            f_info = self._player_folders.get(uname, {})
            m_info = meta_map.get(uname, {})
            local_csv = DATA_CACHE_DIR / uname / f"games_{uname}.csv"
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
        Sync a player's CSV files from Google Drive into local cache and SQLite warehouse,
        performing the SSIS Conditional Split (All / White / Black) and schema normalization.
        """
        if not self._player_folders or username not in self._player_folders:
            try:
                folders = self.drive.list_player_folders()
                self._player_folders = {f["name"]: f for f in folders}
            except Exception as exc:
                print(f"[Warehouse] Could not refresh folders: {exc}")

        # Case-insensitive match if user typed slightly different casing
        matched_name = username
        for k in self._player_folders:
            if k.lower() == username.lower():
                matched_name = k
                break
        username = matched_name

        folder_info = self._player_folders.get(username)
        player_dir = DATA_CACHE_DIR / username
        player_dir.mkdir(parents=True, exist_ok=True)
        games_csv_path = player_dir / f"games_{username}.csv"
        rating_csv_path = player_dir / f"rating_history_{username}.csv"

        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM sync_metadata WHERE username = ?", (username,)
            ).fetchone()
            existing_meta = dict(row) if row else {}

        games_file_meta = {}
        rating_file_meta = {}

        if folder_info and folder_info.get("id"):
            folder_id = folder_info["id"]
            files_map = self.drive.get_player_files_metadata(folder_id)
            games_file_meta = files_map.get(f"games_{username}.csv", {})
            rating_file_meta = files_map.get(f"rating_history_{username}.csv", {})

            # Download games CSV if missing, forced, or newer on Drive
            if games_file_meta.get("id"):
                need_games_dl = (
                    force
                    or not games_csv_path.exists()
                    or games_file_meta.get("modifiedTime")
                    != existing_meta.get("games_modified_time")
                )
                if need_games_dl:
                    self.drive.download_file(games_file_meta["id"], games_csv_path)

            # Download rating history CSV if missing, forced, or newer on Drive
            if rating_file_meta.get("id"):
                need_rating_dl = (
                    force
                    or not rating_csv_path.exists()
                    or rating_file_meta.get("modifiedTime")
                    != existing_meta.get("rating_modified_time")
                )
                if need_rating_dl:
                    self.drive.download_file(rating_file_meta["id"], rating_csv_path)

        # Load and normalize DataFrames
        df_games = self._load_and_normalize_games(username, games_csv_path)
        df_ratings = self._load_and_normalize_ratings(username, rating_csv_path)

        with self._lock:
            self._games_cache[username] = df_games
            self._ratings_cache[username] = df_ratings

        total_games = len(df_games)
        white_games = (
            int((df_games["played_as"] == "white").sum()) if total_games > 0 else 0
        )
        black_games = (
            int((df_games["played_as"] == "black").sum()) if total_games > 0 else 0
        )
        rating_days = len(df_ratings)
        now_iso = datetime.now(timezone.utc).isoformat()

        # Persist to SQLite warehouse tables (mirroring SSIS Destination tables)
        with self._get_conn() as conn:
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
                    folder_info.get("id") if folder_info else existing_meta.get("folder_id"),
                    games_file_meta.get("id", existing_meta.get("games_file_id")),
                    games_file_meta.get(
                        "modifiedTime", existing_meta.get("games_modified_time")
                    ),
                    int(games_file_meta.get("size", 0) or 0),
                    rating_file_meta.get("id", existing_meta.get("rating_file_id")),
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

        # Deduplicate by game_id if any repeated appends occurred in preprocess_and_upload.py
        if "game_id" in df.columns:
            df = df.drop_duplicates(subset=["game_id"], keep="first")

        # Normalize strings and lowercase enums
        for col in ("played_as", "opponent_color", "result", "speed", "status", "source"):
            if col in df.columns:
                df[col] = df[col].astype(str).str.strip().str.lower()

        if "player_name" not in df.columns:
            df["player_name"] = username
        else:
            df["player_name"] = df["player_name"].fillna(username)

        # Normalize boolean columns
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

        # Normalize numeric columns
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

        # Parse timestamps
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
        if username == "__ALL__":
            return self._load_all_synced_players()

        with self._lock:
            has_cached = username in self._games_cache and not force_sync

        if not has_cached:
            self.sync_player(username, force=force_sync)

        with self._lock:
            return (
                self._games_cache.get(username, pd.DataFrame()),
                self._ratings_cache.get(username, pd.DataFrame()),
            )

    def _load_all_synced_players(self) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """Combines all synced players into a unified DataFrame (SSIS Looping Through files.dtsx)."""
        players = self.discover_players()
        dfs = []
        for p in players:
            uname = p["username"]
            local_csv = DATA_CACHE_DIR / uname / f"games_{uname}.csv"
            if local_csv.exists():
                if uname not in self._games_cache:
                    self.sync_player(uname, force=False)
                gdf = self._games_cache.get(uname)
                if gdf is not None and not gdf.empty:
                    dfs.append(gdf)

        if not dfs:
            # If none synced yet, sync the first available player so warehouse isn't empty
            if players:
                first_u = players[0]["username"]
                self.sync_player(first_u, force=False)
                gdf = self._games_cache.get(first_u, pd.DataFrame())
                return gdf, self._ratings_cache.get(first_u, pd.DataFrame())
            return pd.DataFrame(), pd.DataFrame()

        combined = pd.concat(dfs, ignore_index=True).sort_values(
            by="created_dt", ascending=False
        )
        return combined.reset_index(drop=True), pd.DataFrame()

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
        df_raw, df_ratings = self.ensure_player_loaded(username, force_sync=force_sync)

        if df_raw.empty:
            return {
                "username": username,
                "empty": True,
                "summary": self._compute_slice_stats(df_raw),
            }

        # Apply base filters (before color split, so we can show White vs Black side-by-side)
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

        # Compute SSIS Conditional Split stats (White vs Black) on df_base
        df_white = df_base[df_base["played_as"] == "white"]
        df_black = df_base[df_base["played_as"] == "black"]
        color_split = {
            "white": self._compute_slice_stats(df_white),
            "black": self._compute_slice_stats(df_black),
        }

        # Now apply active color filter for the rest of the dashboard
        if color and color.lower() in ("white", "black"):
            df = df_base[df_base["played_as"] == color.lower()]
        else:
            df = df_base

        total = len(df)
        base_stats = self._compute_slice_stats(df)

        # Current rating (latest non-null rating in the filtered set)
        current_rating = None
        valid_ratings = df["player_rating"].dropna()
        if not valid_ratings.empty:
            current_rating = _clean_int(valid_ratings.iloc[0])

        peak_rating = None
        peak_date = None
        if not valid_ratings.empty:
            idx_max = df["player_rating"].idxmax()
            peak_rating = _clean_int(df.loc[idx_max, "player_rating"])
            peak_date = str(df.loc[idx_max, "date_str"])

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

        # 2. Termination / Status Breakdown (Overall + Win vs Lose split)
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
        def _build_openings(sub_df: pd.DataFrame, limit: int = 12) -> List[Dict[str, Any]]:
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

        # 6. Day-of-Week & Hour-of-Day Performance
        dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        dow_stats = []
        hour_stats = []
        if total > 0:
            for d_idx in range(7):
                grp = df[df["dow"] == d_idx]
                cnt = len(grp)
                w = int((grp["result"] == "win").sum()) if cnt > 0 else 0
                dow_stats.append(
                    {
                        "day": dow_names[d_idx],
                        "games": cnt,
                        "win_rate": round(w * 100.0 / cnt, 1) if cnt > 0 else 0.0,
                    }
                )
            for h_idx in range(24):
                grp = df[df["hour"] == h_idx]
                cnt = len(grp)
                w = int((grp["result"] == "win").sum()) if cnt > 0 else 0
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

        # 8. Rating History Time Series (from rating_history_<username>.csv + game fallback)
        rating_history_payload = self._build_rating_history_payload(
            df_ratings, df_raw, date_from=date_from, date_to=date_to
        )

        # 9. Paginated Game Log
        page = max(1, page)
        page_size = max(1, min(100, page_size))
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        page_df = df.iloc[start_idx:end_idx]

        games_list = []
        for _, row in page_df.iterrows():
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
            "recent_games": {
                "page": page,
                "page_size": page_size,
                "total_filtered": total,
                "total_pages": max(1, math.ceil(total / page_size)),
                "items": games_list,
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
                # Downsample to max ~180 points for smooth canvas rendering while keeping exact start/end
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

        # Fallback: compute rating history from games DataFrame if rating_history CSV is empty (e.g., __ALL__)
        if df_games.empty:
            return {"source": "none", "dates": [], "series": {}, "latest_by_category": {}}

        g_df = df_games.dropna(subset=["player_rating"]).copy()
        if date_from:
            g_df = g_df[g_df["date_str"] >= date_from]
        if date_to:
            g_df = g_df[g_df["date_str"] <= date_to]
        if g_df.empty:
            return {"source": "none", "dates": [], "series": {}, "latest_by_category": {}}

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
        """Export filtered dataset as CSV (replacing SSIS Flat File / Excel Destination)."""
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
        return df[export_cols].to_csv(index=False)
