import hmac
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional, Tuple

from fastapi import HTTPException, Request

from app import config


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
    and per-IP / per-player rate limiting for public deployments.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._player_last_trigger: Dict[str, float] = {}
        self._ip_trigger_timestamps: Dict[str, Deque[float]] = defaultdict(deque)

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
    def extract_token(request: Request) -> str:
        header_token = request.headers.get("X-Portal-Auth", "").strip()
        if header_token:
            return header_token
        auth = request.headers.get("Authorization", "").strip()
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        query_token = request.query_params.get("token", "").strip()
        if query_token:
            return query_token
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

    def require_trigger_access(self, request: Request, username: str) -> str:
        """
        Enforces:
          1. Passcode / subset user-token auth (if configured)
          2. Allowed player subset (if ALLOWED_TRIGGER_PLAYERS is configured)
          3. Per-player cooldown & per-IP hourly rate limits
        Returns the caller identity label for audit logging.
        """
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
            if username.strip().lower() not in allowed_lower:
                raise HTTPException(
                    status_code=403,
                    detail=f"Player '{username}' is not in the allowed trigger list ({', '.join(allowed)}).",
                )

        # Rate-limit checks
        now = time.time()
        client_ip = request.client.host if request.client else "unknown"
        norm_user = username.strip().lower()
        cooldown = max(0, int(config.TRIGGER_COOLDOWN_SECONDS))
        max_per_hour = max(1, int(config.TRIGGER_MAX_PER_HOUR))

        with self._lock:
            last_ts = self._player_last_trigger.get(norm_user)
            if last_ts is not None and cooldown > 0:
                elapsed = now - last_ts
                if elapsed < cooldown:
                    wait_s = int(cooldown - elapsed) + 1
                    raise HTTPException(
                        status_code=429,
                        detail=f"Cooldown active for '{username}'. Please wait {wait_s}s before triggering again.",
                    )

            ip_hist = self._ip_trigger_timestamps[client_ip]
            while ip_hist and (now - ip_hist[0]) > 3600:
                ip_hist.popleft()
            if len(ip_hist) >= max_per_hour:
                raise HTTPException(
                    status_code=429,
                    detail=f"Hourly trigger limit ({max_per_hour}/hr) reached for your session. Try again later.",
                )

            # Record trigger timestamps
            self._player_last_trigger[norm_user] = now
            ip_hist.append(now)

        return caller_label

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
