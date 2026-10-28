# Urja Meter API

A clean, read-only REST API in front of the legacy **Urja Meter Ops** portal, which has no API of its
own. Another engineer or program can get meter details, network position, nearby meters and
consumption without ever touching the portal.

- **How the portal actually works:** [`PROTOCOL.md`](PROTOCOL.md)
- **API description (OpenAPI 3.1):** [`openapi.json`](openapi.json). When the service is running, an
  interactive view is at `/docs` (Swagger UI) and the live spec at `/openapi.json`.
- **Reflection:** the last section of this file.

## What it does

| Endpoint | Purpose |
|---|---|
| `GET /meters` | List/filter meters: `q`, `status`, `make`, `phase_type`, `install_type`, `dt_code`, `node`, `build`, `has_issues`, `limit`, `offset` |
| `GET /meters/{id}` | One meter: nameplate, geo, 7-level hierarchy, data-quality flags |
| `GET /meters/{id}/consumption` | Readings with derived per-interval kWh; `from`/`to` window; `aggregate=raw\|daily` |
| `GET /meters/nearby` | Meters within `radius_km` of `lat`/`lng`, nearest first |
| `GET /hierarchy` | The network tree (zone -> circle -> division -> subdivision -> substation -> feeder -> DT) with meter counts and issues |
| `GET /transformers` | Distribution transformers with meter counts and a feeder-consistency check |
| `POST /refresh`, `GET /health` | Force a reload; liveness and data age |

### Structure

```
app/
  portal.py       HTTP client for the portal: login, session renewal, retries, HMAC-signed export
  normalize.py    raw portal records -> clean models; hierarchy repair + tree building
  consumption.py  parse readings, derive interval usage, window, daily rollup
  catalog.py      in-memory index (TTL + stale-on-error), filtering, nearby search, energy cache
  models.py       Pydantic models (these generate openapi.json)
  main.py         FastAPI routes
tests/            23 tests; the portal is faked, so they run offline
scripts/export_openapi.py   regenerates openapi.json
```

## Run it

Python 3.11+ (developed on 3.14).

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export PORTAL_USERNAME='operator@urja.local'            # from the assignment email
export PORTAL_PASSWORD='...'
# optional: PORTAL_BASE_URL, CATALOG_TTL_SECONDS (default 300), ENERGY_TTL_SECONDS (default 300)
# (see .env.example; the service reads real environment variables, it does not load a .env file)

uvicorn app.main:app --port 8000
```

Then open <http://localhost:8000/docs>.

Tests: `pip install -r requirements-dev.txt && pytest`.
Regenerate the spec after changing routes or models: `python -m scripts.export_openapi`.

### Sample request

```bash
curl 'localhost:8000/meters?status=Faulty&limit=1'
curl 'localhost:8000/meters/J100001/consumption?aggregate=daily'
curl 'localhost:8000/meters/nearby?lat=26.9&lng=75.8&radius_km=2&limit=2'
```

```json
{
  "meter_id": "J100001",
  "summary": {"reading_count": 337, "typical_interval_minutes": 30.0, "total_kwh": 138.8,
              "min_voltage": 220.0, "max_voltage": 240.0, "...": "..."},
  "daily": [{"date": "2026-06-24", "kwh": 19.83, "readings": 48}, "..."]
}
```

## Assumptions

- **Timezone.** Portal timestamps have none. I assume India Standard Time (+05:30; Jaipur, no DST) and emit ISO 8601 with the offset.
- **`kwh` is a cumulative register**, so usage is the difference between consecutive readings. A decrease is flagged `register_reset` and not counted as negative usage.
- **A code is not an identity.** Below zone level, codes repeat under different parents, so tree nodes are identified by full path (`Z-01/C-01/D-03`).
- **Blank code/name in the hierarchy means "dropped", not "doesn't exist."** The mapping between names and codes is one-to-one, so I fill the gap from other meters when that is unambiguous.
- **The portal's data is a snapshot** (energy is a fixed 23-30 June 2026 window), so 5-minute freshness is plenty.
- A single shared portal account is used for all callers; this API has no auth of its own.

## Design decisions and trade-offs

- **Use the bulk export, not the UI endpoints.** One signed call returns all 403 meters with hierarchy, geo and install type. The alternative (search pages + geo + detail page per meter) is ~800 requests and needs a `devalue` decoder for two different detail formats. Trade-off: it depends on the signing scheme staying as is.
- **Snapshot in memory, not a database.** 403 meters fit trivially; filtering and nearby search are plain list scans. A DB would be over-engineering at this size.
- **Freshness.** The catalog is reloaded when older than `CATALOG_TTL_SECONDS`. If a reload fails, the *last good data keeps being served* with `X-Catalog-Stale: true` (and `X-Catalog-Age-Seconds`), and retries are throttled to one per 15 s so a struggling portal isn't hammered. If there is nothing cached, the API returns `503 portal_unavailable`. Energy is cached per meter (max 256) with the same fallback.
- **Robustness of the client.** Session renewed before its 1 h expiry, and again on `401` / redirect-to-`/login` (once, then fails with `502 portal_auth_failed` rather than looping); retries with exponential backoff on connection errors and 429/5xx (honouring `Retry-After`); a page cap guards against a bad `total`.
- **Repairing vs. hiding messy data.** Inferred hierarchy values are filled in but every meter carries `data_quality` flags (`inferred_code:circle`, `missing_name:feeder`, `dt_code_mismatch`...), and `/hierarchy` nodes carry `issues`. Consumers can filter on `has_issues=true` or ignore it.
- **Time windows are applied after computing deltas**, so the first reading inside a `from` window still has a correct `interval_kwh`.
- **`typical_interval_minutes` in the summary** because resolution is per-meter (daily vs half-hourly); callers shouldn't assume.

## Intentionally skipped

- **Authentication/rate limiting on this API.** Out of scope for a take-home; it would sit behind a gateway.
- **Parsing the per-meter detail page (`devalue`).** The export carries the same facts; see PROTOCOL.md for the format.
- **Persistence, background refresh, metrics.** The catalog refreshes lazily on request.
- **Web client.** Optional extension; I spent the time on the API's correctness and the protocol notes instead.
- **Anomaly detection beyond flags** (voltage outliers, flat-lines).

## What I'd improve with more time

- Verify actual session-expiry behaviour by letting a session lapse (I only handle the two plausible signals).
- Background refresh so no request ever pays the reload cost; ETag/`If-None-Match` on the list endpoints.
- A small web client: map of meters coloured by status, hierarchy explorer, daily-usage charts.
- A proper test against the live portal, kept separate from the offline suite.
- `nearby` scaling: it is an O(n) haversine scan, fine for ~10^5 meters per request. Beyond that I'd add a grid/geohash or R-tree index, or move to PostGIS. Filtering is also a linear scan; past ~10^5-10^6 rows it should move to a database with indexes on the filter columns.

## Reflection

*(Drafted from what actually happened while building this; edit to your own words before submitting.)*

**What assumptions did you make?** See *Assumptions* above. The riskiest are the IST timezone for naive timestamps, and that a blank hierarchy code/name is a data gap that can be filled from other meters.

**Which part was the most difficult, and how did you get unstuck?** Finding the API at all: the HTML said "0 total" and the browser-visible page had no data. Reading the SvelteKit bundles for `fetch(` calls showed the whole `/portal/*` surface, including the signed export, which wasn't linked from the Meters page. The hierarchy was the second hard part: I first expected a tree, then measured parent-uniqueness per level and found codes repeat, which is why nodes are keyed by path.

**If you had another day, what would you improve?** The items under *What I'd improve*: confirm session expiry, background refresh, and a small map/hierarchy web client.

**What mistake did you make while solving this?** I assumed the hierarchy codes were globally unique, and my first version of the tree treated a blank-code node as a different circle from the real one, splitting 22 meters off into phantom nodes. I only noticed when printing the tree and seeing `Circle 1` next to `C-01`. I also initially wrote a test that expected cached energy for a meter that had never been fetched.

**If you were reviewing your own submission, what would you criticise?** Session-expiry handling is untested against the real portal; the offline fakes encode my guess about what expiry looks like. The data-repair heuristic assumes name<->code is one-to-one, which held for this dataset but isn't guaranteed. Filtering and `nearby` are linear scans. And there's no auth on the API itself.
