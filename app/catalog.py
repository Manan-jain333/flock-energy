"""In-memory index of the portal's data, with TTL freshness and stale-on-error behaviour."""
import asyncio
import time
from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt
from typing import Callable

from .models import HierarchyNode, Meter, NearbyMeter, Transformer
from .normalize import build_transformers, build_tree, normalize_meters
from .portal import PortalClient, PortalError

RETRY_AFTER_FAILURE = 15.0  # don't hammer a struggling portal while serving stale data


@dataclass
class Snapshot:
    meters: list[Meter]
    by_id: dict[str, Meter]
    transformers: list[Transformer]
    tree: list[HierarchyNode]
    loaded_at: float


class Catalog:
    def __init__(self, portal: PortalClient, ttl: float, clock: Callable[[], float] = time.monotonic):
        self._portal = portal
        self._ttl = ttl
        self._clock = clock
        self._snapshot: Snapshot | None = None
        self._last_failure: float | None = None
        self._lock = asyncio.Lock()
        self.last_error: str | None = None

    def age(self) -> float | None:
        return None if self._snapshot is None else self._clock() - self._snapshot.loaded_at

    def _fresh(self) -> bool:
        age = self.age()
        return age is not None and age < self._ttl

    async def get(self, force: bool = False) -> tuple[Snapshot, bool]:
        """Return (snapshot, is_stale). Raises PortalError only if there is nothing to serve."""
        if self._fresh() and not force:
            return self._snapshot, False  # type: ignore[return-value]
        async with self._lock:
            if self._fresh() and not force:
                return self._snapshot, False  # type: ignore[return-value]
            recently_failed = self._last_failure is not None and self._clock() - self._last_failure < RETRY_AFTER_FAILURE
            if self._snapshot is not None and recently_failed and not force:
                return self._snapshot, True
            try:
                self._snapshot = await self._load()
                self._last_failure, self.last_error = None, None
                return self._snapshot, False
            except PortalError as exc:
                self._last_failure, self.last_error = self._clock(), str(exc)
                if self._snapshot is None:
                    raise
                return self._snapshot, True

    async def _load(self) -> Snapshot:
        raw_meters, raw_dts = await asyncio.gather(self._portal.fetch_meters(), self._portal.fetch_transformers())
        meters = normalize_meters(raw_meters)
        return Snapshot(
            meters=meters,
            by_id={m.meter_id: m for m in meters},
            transformers=build_transformers(raw_dts, meters),
            tree=build_tree(meters),
            loaded_at=self._clock(),
        )


def filter_meters(
    meters: list[Meter],
    *,
    q: str | None = None,
    status: str | None = None,
    make: str | None = None,
    phase_type: str | None = None,
    install_type: str | None = None,
    dt_code: str | None = None,
    node: str | None = None,
    build: str | None = None,
    has_issues: bool | None = None,
) -> list[Meter]:
    def match(m: Meter) -> bool:
        if q and q.lower() not in m.meter_id.lower() and q.lower() not in m.serial_no.lower():
            return False
        if status and m.install_status.lower() != status.lower():
            return False
        if make and m.make.lower() != make.lower():
            return False
        if phase_type and m.phase_type.lower() != phase_type.lower():
            return False
        if install_type and m.install_type.lower() != install_type.lower():
            return False
        if dt_code and m.dt_code != dt_code:
            return False
        if build and m.build.lower() != build.lower():
            return False
        if has_issues is not None and bool(m.data_quality) != has_issues:
            return False
        if node and not any(lvl.node_id == node for lvl in m.hierarchy):
            return False
        return True

    return [m for m in meters if match(m)]


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    dlat, dlng = radians(lat2 - lat1), radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return 2 * 6371.0088 * asin(sqrt(a))


def nearby(meters: list[Meter], lat: float, lng: float, radius_km: float, limit: int) -> list[NearbyMeter]:
    hits = [(haversine_km(lat, lng, m.geo.lat, m.geo.lng), m) for m in meters]
    hits = sorted((h for h in hits if h[0] <= radius_km), key=lambda h: h[0])
    return [NearbyMeter(distance_km=round(d, 3), meter=m) for d, m in hits[:limit]]


class EnergyCache:
    """Small per-meter cache of raw readings; serves a stale copy if the portal fails."""

    def __init__(self, portal: PortalClient, ttl: float, max_entries: int = 256, clock=time.monotonic):
        self._portal = portal
        self._ttl = ttl
        self._max = max_entries
        self._clock = clock
        self._entries: dict[str, tuple[float, list[dict]]] = {}

    async def get(self, meter_id: str) -> tuple[list[dict], bool]:
        cached = self._entries.get(meter_id)
        if cached and self._clock() - cached[0] < self._ttl:
            return cached[1], False
        try:
            rows = await self._portal.fetch_energy(meter_id)
        except PortalError:
            if cached:
                return cached[1], True
            raise
        if len(self._entries) >= self._max:
            self._entries.pop(min(self._entries, key=lambda k: self._entries[k][0]))
        self._entries[meter_id] = (self._clock(), rows)
        return rows, False
