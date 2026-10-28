from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from .catalog import Catalog, EnergyCache, Snapshot, filter_meters, nearby
from .config import Settings
from .consumption import IST, build_consumption
from .models import Consumption, Geo, HierarchyNode, Meter, MeterPage, NearbyResult, Transformer
from .portal import PortalAuthError, PortalClient, PortalError, PortalNotFound, PortalUnavailable

DESCRIPTION = """
A clean, read-only API over the legacy **Urja Meter Ops** portal.

Data is loaded from the portal's bulk export and cached in memory. Responses carry
`X-Catalog-Age-Seconds` and `X-Catalog-Stale` headers: `true` means the portal could not be
reached and the last known data is being served.
"""


def create_app(portal: PortalClient | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        client = portal or PortalClient(
            settings.portal_base_url, settings.portal_username, settings.portal_password
        )
        app.state.portal = client
        app.state.catalog = Catalog(client, settings.catalog_ttl)
        app.state.energy = EnergyCache(client, settings.energy_ttl)
        yield
        await client.aclose()

    app = FastAPI(title="Urja Meter API", version="1.0.0", description=DESCRIPTION, lifespan=lifespan)

    @app.exception_handler(PortalError)
    async def portal_error(_: Request, exc: PortalError):
        status = {PortalUnavailable: 503, PortalNotFound: 404}.get(type(exc), 502)
        code = {
            PortalUnavailable: "portal_unavailable",
            PortalAuthError: "portal_auth_failed",
            PortalNotFound: "not_found",
        }.get(type(exc), "portal_error")
        return JSONResponse({"error": code, "message": str(exc)}, status_code=status)

    async def snapshot(request: Request, response: Response) -> Snapshot:
        catalog: Catalog = request.app.state.catalog
        snap, stale = await catalog.get()
        response.headers["X-Catalog-Age-Seconds"] = str(round(catalog.age() or 0))
        response.headers["X-Catalog-Stale"] = str(stale).lower()
        return snap

    Snap = Annotated[Snapshot, Depends(snapshot)]

    def get_meter(snap: Snapshot, meter_id: str) -> Meter:
        meter = snap.by_id.get(meter_id)
        if meter is None:
            raise HTTPException(404, detail=f"Meter {meter_id} not found")
        return meter

    @app.get("/health", tags=["meta"], summary="Liveness and data freshness")
    async def health(request: Request):
        catalog: Catalog = request.app.state.catalog
        return {"status": "ok", "catalog_age_seconds": catalog.age(), "last_portal_error": catalog.last_error}

    @app.post("/refresh", tags=["meta"], summary="Force a reload from the portal")
    async def refresh(request: Request):
        snap, stale = await request.app.state.catalog.get(force=True)
        return {"meters": len(snap.meters), "stale": stale}

    @app.get("/meters", response_model=MeterPage, tags=["meters"], summary="List and filter meters")
    async def list_meters(
        snap: Snap,
        q: Annotated[str | None, Query(description="Substring of meter id or serial number.")] = None,
        status: Annotated[str | None, Query(description="Installed, Faulty or Decommissioned.")] = None,
        make: str | None = None,
        phase_type: Annotated[str | None, Query(description="single or three.")] = None,
        install_type: Annotated[str | None, Query(description="'CT Operated' or 'Whole Current'.")] = None,
        dt_code: str | None = None,
        node: Annotated[str | None, Query(description="A node_id from /hierarchy; matches meters beneath it.")] = None,
        build: Annotated[str | None, Query(description="Portal record format: legacy or v2.")] = None,
        has_issues: Annotated[bool | None, Query(description="Only meters with (or without) data_quality issues.")] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        found = filter_meters(
            snap.meters, q=q, status=status, make=make, phase_type=phase_type, install_type=install_type,
            dt_code=dt_code, node=node, build=build, has_issues=has_issues,
        )
        return MeterPage(items=found[offset : offset + limit], total=len(found), limit=limit, offset=offset)

    @app.get("/meters/nearby", response_model=NearbyResult, tags=["meters"], summary="Meters near a point")
    async def meters_nearby(
        snap: Snap,
        lat: Annotated[float, Query(ge=-90, le=90)],
        lng: Annotated[float, Query(ge=-180, le=180)],
        radius_km: Annotated[float, Query(gt=0, le=500)] = 5,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ):
        return NearbyResult(
            center=Geo(lat=lat, lng=lng), radius_km=radius_km, items=nearby(snap.meters, lat, lng, radius_km, limit)
        )

    @app.get("/meters/{meter_id}", response_model=Meter, tags=["meters"], summary="One meter")
    async def meter_detail(snap: Snap, meter_id: str):
        return get_meter(snap, meter_id)

    @app.get(
        "/meters/{meter_id}/consumption",
        response_model=Consumption,
        tags=["meters"],
        summary="Readings and derived usage for a meter",
    )
    async def meter_consumption(
        request: Request,
        response: Response,
        snap: Snap,
        meter_id: str,
        start: Annotated[datetime | None, Query(alias="from", description="ISO 8601; naive = Asia/Kolkata.")] = None,
        end: Annotated[datetime | None, Query(alias="to", description="ISO 8601; naive = Asia/Kolkata.")] = None,
        aggregate: Literal["raw", "daily"] = "raw",
    ):
        get_meter(snap, meter_id)  # 404 before bothering the portal
        rows, stale = await request.app.state.energy.get(meter_id)
        if stale:
            response.headers["X-Catalog-Stale"] = "true"
        start = start.replace(tzinfo=IST) if start and start.tzinfo is None else start
        end = end.replace(tzinfo=IST) if end and end.tzinfo is None else end
        return build_consumption(meter_id, rows, start, end, aggregate)

    @app.get("/hierarchy", response_model=list[HierarchyNode], tags=["network"], summary="Network tree")
    async def hierarchy(snap: Snap):
        return snap.tree

    @app.get("/transformers", response_model=list[Transformer], tags=["network"], summary="Distribution transformers")
    async def transformers(snap: Snap):
        return snap.transformers

    return app


app = create_app()
