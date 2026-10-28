import hashlib
import hmac

import httpx
import pytest

from app.portal import PortalAuthError, PortalClient, PortalNotFound, PortalUnavailable

SECRET = "s3cret"


class FakePortal:
    """Just enough of the portal to exercise login, expiry, retries and export signing."""

    def __init__(self):
        self.logins = 0
        self.session_valid = False
        self.fail_next: list[int] = []  # status codes to return before behaving
        self.export_requests: list[httpx.Request] = []
        self.accept_credentials = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/login":
            assert request.headers["origin"] == "https://portal.test"
            if not self.accept_credentials:
                return httpx.Response(200, json={"type": "failure", "status": 400})
            self.logins += 1
            self.session_valid = True
            return httpx.Response(200, json={"type": "redirect", "status": 303, "location": "/meters"})
        if not self.session_valid:
            return httpx.Response(303, headers={"location": "/login"})
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0))
        if path == "/portal/keys":
            return httpx.Response(200, json={"data": {"signingSecret": SECRET}})
        if path == "/portal/export":
            self.export_requests.append(request)
            page = int(request.url.params["page"])
            rows = [{"meterId": f"M{i}"} for i in range(page * 2 - 2, page * 2)]
            return httpx.Response(200, json={"data": rows, "total": 5})
        if path == "/portal/meters/GONE/energy":
            return httpx.Response(404, json={"error": "not_found"})
        if path.endswith("/energy"):
            return httpx.Response(200, json={"data": [{"timestamp": "x"}]})
        return httpx.Response(404)


async def no_sleep(_: float) -> None:
    return None


def make_client(portal: FakePortal, **kw) -> PortalClient:
    return PortalClient(
        "https://portal.test", "u", "p", transport=httpx.MockTransport(portal), sleep=no_sleep, **kw
    )


async def test_logs_in_lazily_and_fetches_energy():
    portal = FakePortal()
    client = make_client(portal)
    assert await client.fetch_energy("J1") == [{"timestamp": "x"}]
    assert portal.logins == 1


async def test_bad_credentials_raise_auth_error():
    portal = FakePortal()
    portal.accept_credentials = False
    with pytest.raises(PortalAuthError):
        await make_client(portal).fetch_energy("J1")


async def test_relogs_in_when_session_is_revoked_server_side():
    portal = FakePortal()
    client = make_client(portal)
    await client.fetch_energy("J1")
    portal.session_valid = False  # e.g. portal restarted or cookie expired early
    await client.fetch_energy("J1")
    assert portal.logins == 2


async def test_renews_session_proactively_before_it_expires():
    now = [0.0]
    portal = FakePortal()
    client = make_client(portal, session_ttl=100, clock=lambda: now[0])
    await client.fetch_energy("J1")
    now[0] = 101
    await client.fetch_energy("J1")
    assert portal.logins == 2


async def test_retries_transient_5xx_then_succeeds():
    portal = FakePortal()
    client = make_client(portal)
    await client.fetch_energy("J1")
    portal.fail_next = [503, 502]
    assert await client.fetch_energy("J1")


async def test_gives_up_with_unavailable_after_retries():
    portal = FakePortal()
    client = make_client(portal, max_retries=2)
    await client.fetch_energy("J1")
    portal.fail_next = [503] * 10
    with pytest.raises(PortalUnavailable):
        await client.fetch_energy("J1")


async def test_404_maps_to_not_found():
    with pytest.raises(PortalNotFound):
        await make_client(FakePortal()).fetch_energy("GONE")


async def test_export_is_signed_and_paged():
    portal = FakePortal()
    meters = await make_client(portal).fetch_meters()
    assert [m["meterId"] for m in meters] == ["M0", "M1", "M2", "M3", "M4", "M5"][: len(meters)]
    assert len(meters) >= 5  # stopped once total was reached
    req = portal.export_requests[0]
    message = "\n".join(["GET", "/portal/export", "page=1", req.headers["x-timestamp"]])
    expected = hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()
    assert req.headers["x-signature"] == expected
