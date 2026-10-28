"""Client for the legacy Urja Meter Ops portal. See PROTOCOL.md for how it works."""
import asyncio
import hashlib
import hmac
import time
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode

import httpx


class PortalError(Exception):
    """Base class for anything that goes wrong talking to the portal."""


class PortalAuthError(PortalError):
    """Login was rejected, or the session could not be re-established."""


class PortalUnavailable(PortalError):
    """The portal is down, timing out, or persistently returning 5xx/429."""


class PortalNotFound(PortalError):
    """The portal answered 404 for the requested resource."""


# Sessions last 1h (Max-Age=3600); renew a bit early rather than wait for a failure.
SESSION_TTL = 3000.0
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_PAGES = 100  # safety net so a misbehaving `total` can't loop forever


class PortalClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 20.0,
        max_retries: int = 3,
        backoff: float = 0.5,
        session_ttl: float = SESSION_TTL,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._http = httpx.AsyncClient(
            base_url=self._base_url, transport=transport, timeout=timeout, follow_redirects=False
        )
        self._max_retries = max_retries
        self._backoff = backoff
        self._session_ttl = session_ttl
        self._clock = clock
        self._sleep = sleep
        self._logged_in_at: float | None = None
        self._login_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- auth -----------------------------------------------------------------

    async def _login(self) -> None:
        if not self._username or not self._password:
            raise PortalAuthError(
                "PORTAL_USERNAME / PORTAL_PASSWORD are not set in the environment of the server process"
            )
        try:
            resp = await self._http.post(
                "/login",
                data={"email": self._username, "password": self._password},
                # SvelteKit rejects cross-origin form posts, so Origin is required.
                headers={"Origin": self._base_url, "Accept": "application/json"},
            )
        except httpx.TransportError as exc:
            raise PortalUnavailable(f"login failed: {exc}") from exc
        if resp.status_code >= 500:
            raise PortalUnavailable(f"login returned {resp.status_code}")
        try:
            result = resp.json()
        except ValueError:
            result = {}
        # Success is a SvelteKit "redirect" result; bad credentials come back as "failure".
        if result.get("type") != "redirect":
            raise PortalAuthError(f"portal rejected the credentials for user {self._username!r}")
        self._logged_in_at = self._clock()

    async def _ensure_session(self, force: bool = False) -> None:
        async with self._login_lock:
            expired = self._logged_in_at is None or self._clock() - self._logged_in_at > self._session_ttl
            if force or expired:
                self._http.cookies.clear()
                await self._login()

    @staticmethod
    def _is_auth_failure(resp: httpx.Response) -> bool:
        if resp.status_code == 401:
            return True
        return resp.is_redirect and "/login" in resp.headers.get("location", "")

    # ---- transport ------------------------------------------------------------

    async def _request(
        self, path: str, params: dict[str, Any] | None = None, *, signed: bool = False
    ) -> httpx.Response:
        query = urlencode(params or {})
        reauthed = False
        last_error = "unknown error"
        for attempt in range(self._max_retries + 1):
            await self._ensure_session()
            headers = await self._sign("GET", path, query) if signed else {}
            try:
                resp = await self._http.get(f"{path}?{query}" if query else path, headers=headers)
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                await self._sleep(self._backoff * 2**attempt)
                continue
            if self._is_auth_failure(resp):
                if reauthed:
                    raise PortalAuthError("portal keeps rejecting the session")
                reauthed = True
                await self._ensure_session(force=True)
                continue
            if resp.status_code in RETRY_STATUSES:
                last_error = f"HTTP {resp.status_code}"
                retry_after = resp.headers.get("retry-after", "")
                delay = float(retry_after) if retry_after.isdigit() else self._backoff * 2**attempt
                await self._sleep(delay)
                continue
            if resp.status_code == 404:
                raise PortalNotFound(path)
            if resp.status_code >= 400:
                raise PortalError(f"{path} returned {resp.status_code}: {resp.text[:200]}")
            return resp
        raise PortalUnavailable(f"{path} failed after {self._max_retries + 1} attempts ({last_error})")

    async def _sign(self, method: str, path: str, query: str) -> dict[str, str]:
        """Headers for the HMAC-signed /portal/export endpoint."""
        secret = (await self._request("/portal/keys")).json()["data"]["signingSecret"]
        timestamp = str(int(time.time()))
        message = "\n".join([method, path, query, timestamp])
        signature = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        return {"x-timestamp": timestamp, "x-signature": signature}

    # ---- data -----------------------------------------------------------------

    async def _paged(self, path: str, signed: bool = False) -> list[dict]:
        items: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            body = (await self._request(path, {"page": page}, signed=signed)).json()
            batch = body.get("data") or []
            items.extend(batch)
            if not batch or len(items) >= body.get("total", len(items)):
                break
        return items

    async def fetch_meters(self) -> list[dict]:
        """Every meter with hierarchy and geo, via the signed bulk export."""
        return await self._paged("/portal/export", signed=True)

    async def fetch_transformers(self) -> list[dict]:
        return await self._paged("/portal/dts")

    async def fetch_energy(self, meter_id: str) -> list[dict]:
        resp = await self._request(f"/portal/meters/{meter_id}/energy")
        return resp.json().get("data", [])
