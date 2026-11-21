"""Roblox web API helpers: cookie validation, auth tickets, avatars."""

import time
import requests

AUTH_TICKET_URL = "https://auth.roblox.com/v1/authentication-ticket"
AUTHENTICATED_USER_URL = "https://users.roblox.com/v1/users/authenticated"
AVATAR_HEADSHOT_URL = "https://thumbnails.roblox.com/v1/users/avatar-headshot"

_HEADERS = {
    "Content-Type": "application/json",
    "Referer": "https://www.roblox.com/",
    "Origin": "https://www.roblox.com",
    "User-Agent": "Roblox/WinInet",
}


class ApiError(Exception):
    pass


def normalize_cookie(raw: str) -> str:
    """Accept pasted cookies in common formats and return the bare cookie value.

    Handles: bare ``_|WARNING...``, ``.ROBLOSECURITY=_|WARNING...``,
    and ``username:password:_|WARNING...`` combo pastes.
    """
    raw = raw.strip().strip('"').strip("'")
    if ".ROBLOSECURITY=" in raw:
        raw = raw.split(".ROBLOSECURITY=", 1)[1]
    idx = raw.find("_|WARNING")
    if idx != -1:
        raw = raw[idx:]
    return raw.strip().rstrip(";").strip()


def _session(cookie: str) -> requests.Session:
    s = requests.Session()
    s.headers.update(_HEADERS)
    s.cookies.set(".ROBLOSECURITY", cookie, domain=".roblox.com")
    return s


def get_authenticated_user(cookie: str) -> dict:
    """Validate a cookie. Returns {id, name, displayName}. Raises ApiError."""
    s = _session(cookie)
    try:
        r = s.get(AUTHENTICATED_USER_URL, timeout=15)
    except requests.RequestException as e:
        raise ApiError(f"Network error: {e}") from e
    if r.status_code == 401:
        raise ApiError("Cookie is invalid or expired")
    if not r.ok:
        raise ApiError(f"Validation failed (HTTP {r.status_code})")
    try:
        return r.json()
    except ValueError as e:
        raise ApiError("Unexpected response from Roblox") from e


def get_avatar_headshot(user_id: int, size: int = 100) -> bytes | None:
    """Return PNG bytes for the user's headshot, or None."""
    try:
        r = requests.get(
            AVATAR_HEADSHOT_URL,
            params={
                "userIds": user_id,
                "size": f"{size}x{size}",
                "format": "Png",
                "isCircular": "false",
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()["data"]
        if not data or not data[0].get("imageUrl"):
            return None
        img = requests.get(data[0]["imageUrl"], timeout=15)
        img.raise_for_status()
        return img.content
    except Exception:
        return None


def get_auth_ticket(cookie: str, retries: int = 3) -> str:
    """Mint a single-use auth ticket for launching the client."""
    s = _session(cookie)
    csrf = None
    last_err = ""
    for _ in range(retries):
        headers = {"X-CSRF-TOKEN": csrf} if csrf else {}
        try:
            r = s.post(AUTH_TICKET_URL, json={}, headers=headers, timeout=15)
        except requests.RequestException as e:
            raise ApiError(f"Network error: {e}") from e

        if r.status_code == 403 and "x-csrf-token" in r.headers:
            csrf = r.headers["x-csrf-token"]
            continue

        if r.ok:
            ticket = r.headers.get("rbx-authentication-ticket")
            if not ticket:
                try:
                    ticket = r.json().get("authenticationTicket")
                except ValueError:
                    ticket = None
            if ticket:
                return ticket
            last_err = "No ticket in response"
        elif r.status_code == 401:
            raise ApiError("Cookie is invalid or expired")
        else:
            last_err = f"HTTP {r.status_code}"
        time.sleep(0.5)

    raise ApiError(f"Could not get auth ticket ({last_err})")
