import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.portal import PortalUnavailable
from tests.test_domain import raw_meter


class StubPortal:
    def __init__(self):
        near = raw_meter("J1")
        far = raw_meter("J2", circle=("C-02", "Circle 2"))
        far["geo"] = {"lat": 27.5, "lng": 76.5}
        far["installStatus"] = "Faulty"
        self.meters = [near, far]
        self.down = False

    async def fetch_meters(self):
        if self.down:
            raise PortalUnavailable("down")
        return self.meters

    async def fetch_transformers(self):
        return [{"code": "DT-001", "name": "DT 1", "feederCode": "F-001", "capacityKva": 100}]

    async def fetch_energy(self, meter_id):
        if self.down:
            raise PortalUnavailable("down")
        return [
            {"timestamp": "24/06/2026 00:00", "kwh": "1", "kvah": "1", "voltR": "230"},
            {"timestamp": "24/06/2026 00:30", "kwh": "2", "kvah": "1", "voltR": "230"},
        ]

    async def aclose(self):
        pass


@pytest.fixture
def stub():
    return StubPortal()


@pytest.fixture
def client(stub):
    settings = Settings("https://x", "u", "p", catalog_ttl=0, energy_ttl=0)  # ttl 0: always try to refresh
    with TestClient(create_app(stub, settings)) as c:
        yield c


def test_list_and_filter(client):
    assert client.get("/meters").json()["total"] == 2
    assert [m["meter_id"] for m in client.get("/meters?status=faulty").json()["items"]] == ["J2"]
    assert client.get("/meters?q=SJ1").json()["total"] == 1
    assert client.get("/meters?node=Z-01/C-02").json()["total"] == 1
    assert client.get("/meters?limit=1&offset=1").json()["items"][0]["meter_id"] == "J2"


def test_meter_detail_and_404(client):
    assert client.get("/meters/J1").json()["hierarchy"][0]["node_id"] == "Z-01"
    assert client.get("/meters/NOPE").status_code == 404
    assert client.get("/meters/NOPE/consumption").status_code == 404


def test_nearby_orders_by_distance_and_respects_radius(client):
    body = client.get("/meters/nearby?lat=26.9&lng=75.8&radius_km=5").json()
    assert [i["meter"]["meter_id"] for i in body["items"]] == ["J1"]
    wide = client.get("/meters/nearby?lat=26.9&lng=75.8&radius_km=200").json()
    assert [i["meter"]["meter_id"] for i in wide["items"]] == ["J1", "J2"]
    assert client.get("/meters/nearby?lat=95&lng=0").status_code == 422


def test_consumption(client):
    body = client.get("/meters/J1/consumption").json()
    assert body["summary"]["total_kwh"] == 1
    assert body["readings"][1]["interval_kwh"] == 1


def test_hierarchy_and_transformers(client):
    tree = client.get("/hierarchy").json()
    assert tree[0]["meter_count"] == 2 and len(tree[0]["children"]) == 2
    assert client.get("/transformers").json()[0]["meter_count"] == 2


def test_serves_stale_data_when_portal_goes_down(client, stub):
    assert client.get("/meters").headers["x-catalog-stale"] == "false"
    assert client.get("/meters/J1/consumption").status_code == 200  # warm the energy cache
    stub.down = True
    resp = client.get("/meters", params={"limit": 5})
    assert resp.status_code == 200
    assert resp.headers["x-catalog-stale"] == "true"
    assert client.get("/meters/J1/consumption").status_code == 200  # cached energy also falls back
    assert client.get("/meters/J2/consumption").status_code == 503  # never cached: nothing to serve


def test_503_when_portal_down_and_nothing_cached(stub):
    stub.down = True
    settings = Settings("https://x", "u", "p")
    with TestClient(create_app(stub, settings)) as c:
        resp = c.get("/meters")
        assert resp.status_code == 503
        assert resp.json()["error"] == "portal_unavailable"
