import hmac
import re
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional, Set

from fastapi import HTTPException, Request

from app import config

USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{2,30}$")
MAX_TRACKED_IPS = 4096


def validate_player_username(username: str, allow_all: bool = False) -> str:
    """
    Strictly validates a Lichess username against path traversal, shell metacharacters,
    and malformed identifiers (SEC-01, SEC-02).
    """
    cleaned = (username or "").strip()
    if allow_all and cleaned == "__ALL__":
        return "__ALL__"
    if not USERNAME_RE.fullmatch(cleaned):
        raise HTTPException(
            status_code=400,
            detail="Invalid Lichess username. Use 2-30 letters, digits, underscores, or hyphens.",
        )
    return cleaned


def _parse_csv_set(raw: str) -> List[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _parse_user_tokens(raw: str) -> Dict[str, str]:
    """Parse 'alice:secret1,bob:secret2' into {secret: username}."""
    tokens: Dict[str, str] = {}
    for entry in _parse_csv_set(raw):
        if ":" in entry:
            label, secret = entry.split(":", 1)
            label = label.strip()
            secret = secret.strip()
            if label and secret:
                tokens[secret] = label
    return tokens


class AccessGuard:
    """
    Handles passcode / per-user token authentication, player allowlists,
    and proxy-aware per-IP / per-player rate limiting for public deployments.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._player_last_trigger: Dict[str, float] = {}
        self._ip_trigger_timestamps: Dict[str, Deque[float]] = defaultdict(deque)
        self._in_flight_triggers: Set[str] = set()

    @staticmethod
    def _trigger_passcodes() -> List[str]:
        return _parse_csv_set(config.PORTAL_TRIGGER_PASSCODE)

    @staticmethod
    def _user_tokens() -> Dict[str, str]:
        return _parse_user_tokens(config.PORTAL_USER_TOKENS)

    @staticmethod
    def _view_passcodes() -> List[str]:
        return _parse_csv_set(config.PORTAL_VIEW_PASSCODE)

    @staticmethod
    def allowed_players() -> List[str]:
        return _parse_csv_set(config.ALLOWED_TRIGGER_PLAYERS)

    def is_trigger_protected(self) -> bool:
        return bool(self._trigger_passcodes() or self._user_tokens())

    def is_view_protected(self) -> bool:
        return bool(self._view_passcodes())

    @staticmethod
    def extract_client_ip(request: Request) -> str:
        if getattr(config, "TRUST_PROXY_HEADERS", True):
            cf_ip = request.headers.get("CF-Connecting-IP", "").strip()
            if cf_ip:
                return cf_ip
            xff = request.headers.get("X-Forwarded-For", "").strip()
            if xff:
                first_hop = xff.split(",")[0].strip()
                if first_hop:
                    return first_hop
        return request.client.host if request.client else "unknown"

    @staticmethod
    def extract_token(request: Request) -> str:
        header_token = request.headers.get("X-Portal-Auth", "").strip()
        if header_token:
            return header_token
        auth = request.headers.get("Authorization", "").strip()
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return ""

    def authenticate_trigger_token(self, token: str) -> Optional[str]:
        """Returns authorized label/user if token is valid, else None."""
        if not token:
            return None

        for secret, label in self._user_tokens().items():
            if hmac.compare_digest(token, secret):
                return label

        for code in self._trigger_passcodes():
            if hmac.compare_digest(token, code):
                return "authorized-user"

        for code in self._view_passcodes():
            if hmac.compare_digest(token, code):
                return "admin"

        return None

    def authenticate_view_token(self, token: str) -> bool:
        if not self.is_view_protected():
            return True
        if not token:
            return False
        for code in self._view_passcodes():
            if hmac.compare_digest(token, code):
                return True
        return self.authenticate_trigger_token(token) is not None

    def require_view_access(self, request: Request) -> None:
        if not self.is_view_protected():
            return
        token = self.extract_token(request)
        if not self.authenticate_view_token(token):
            raise HTTPException(
                status_code=401,
                detail="Portal passcode required to view analytics.",
            )

    def _prune_stale_unlocked(self, now: float, cooldown: int) -> None:
        stale_players = [
            u
            for u, ts in self._player_last_trigger.items()
            if (now - ts) > max(cooldown, 3600)
        ]
        for u in stale_players:
            del self._player_last_trigger[u]

        empty_ips = []
        for ip, q in self._ip_trigger_timestamps.items():
            while q and (now - q[0]) > 3600:
                q.popleft()
            if not q:
                empty_ips.append(ip)
        for ip in empty_ips:
            del self._ip_trigger_timestamps[ip]

        if len(self._ip_trigger_timestamps) > MAX_TRACKED_IPS:
            oldest_ip = min(
                self._ip_trigger_timestamps,
                key=lambda k: self._ip_trigger_timestamps[k][0]
                if self._ip_trigger_timestamps[k]
                else 0.0,
            )
            del self._ip_trigger_timestamps[oldest_ip]

    def require_trigger_access(self, request: Request, username: str) -> str:
        """
        Enforces:
          1. Strict username format validation
          2. Passcode / subset user-token auth (if configured)
          3. Allowed player subset (if ALLOWED_TRIGGER_PLAYERS is configured)
          4. Per-player cooldown & per-IP hourly rate limits
        Reserves the trigger slot; call rollback_trigger_slot() if upstream dispatch fails.
        """
        clean_username = validate_player_username(username, allow_all=False)
        token = self.extract_token(request)
        caller_label = "public"

        if self.is_trigger_protected():
            matched = self.authenticate_trigger_token(token)
            if not matched:
                raise HTTPException(
                    status_code=401,
                    detail="Access key required to trigger GitHub Actions pipelines.",
                )
            caller_label = matched

        allowed = self.allowed_players()
        if allowed:
            allowed_lower = {u.lower() for u in allowed}
            if clean_username.lower() not in allowed_lower:
                raise HTTPException(
                    status_code=403,
                    detail=f"Player '{clean_username}' is not in the allowed trigger list ({', '.join(allowed)}).",
                )

        now = time.time()
        client_ip = self.extract_client_ip(request)
        norm_user = clean_username.lower()
        cooldown = max(0, int(config.TRIGGER_COOLDOWN_SECONDS))
        max_per_hour = max(1, int(config.TRIGGER_MAX_PER_HOUR))

        with self._lock:
            self._prune_stale_unlocked(now, cooldown)

            if norm_user in self._in_flight_triggers:
                raise HTTPException(
                    status_code=429,
                    detail=f"A pipeline trigger for '{clean_username}' is already in progress.",
                )

            last_ts = self._player_last_trigger.get(norm_user)
            if last_ts is not None and cooldown > 0:
                elapsed = now - last_ts
                if elapsed < cooldown:
                    wait_s = int(cooldown - elapsed) + 1
                    raise HTTPException(
                        status_code=429,
                        detail=f"Cooldown active for '{clean_username}'. Please wait {wait_s}s before triggering again.",
                    )

            ip_hist = self._ip_trigger_timestamps[client_ip]
            if len(ip_hist) >= max_per_hour:
                raise HTTPException(
                    status_code=429,
                    detail=f"Hourly trigger limit ({max_per_hour}/hr) reached for your session. Try again later.",
                )

            self._in_flight_triggers.add(norm_user)
            self._player_last_trigger[norm_user] = now
            ip_hist.append(now)

        return caller_label

    def release_trigger_slot(
        self, request: Request, username: str, succeeded: bool = True
    ) -> None:
        """
        Releases the in-flight lock for username, and if succeeded=False,
        rolls back the cooldown and hourly rate-limit consumption so failed
        lookups/dispatches do not penalize the user.
        """
        norm_user = (username or "").strip().lower()
        client_ip = self.extract_client_ip(request)
        with self._lock:
            self._in_flight_triggers.discard(norm_user)
            if not succeeded:
                self._player_last_trigger.pop(norm_user, None)
                ip_hist = self._ip_trigger_timestamps.get(client_ip)
                if ip_hist:
                    ip_hist.pop()
                    if not ip_hist:
                        self._ip_trigger_timestamps.pop(client_ip, None)

    def get_status(self, request: Request) -> Dict[str, object]:
        token = self.extract_token(request)
        trigger_protected = self.is_trigger_protected()
        view_protected = self.is_view_protected()
        trigger_user = (
            self.authenticate_trigger_token(token)
            if trigger_protected
            else "open"
        )
        view_ok = self.authenticate_view_token(token)
        return {
            "trigger_protected": trigger_protected,
            "trigger_authorized": (not trigger_protected) or (trigger_user is not None),
            "authorized_user": trigger_user,
            "view_protected": view_protected,
            "view_authorized": view_ok,
            "allowed_players": self.allowed_players(),
            "cooldown_seconds": config.TRIGGER_COOLDOWN_SECONDS,
            "max_triggers_per_hour": config.TRIGGER_MAX_PER_HOUR,
        }
