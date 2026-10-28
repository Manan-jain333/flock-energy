# How the Urja Meter Ops portal works

Findings from exploring `https://urja-ops.flockenergy.tech` as a normal logged-in user. Everything here
was observed with read-only requests, using the same calls the portal's own pages make. Nothing was
brute-forced or modified.

## 1. Shape of the system

A SvelteKit app. The HTML pages are server-rendered shells; the tables are filled in **client-side**
from a small JSON API under `/portal/*` (the page says "0 total" in the raw HTML, then fills in).
That API is not documented anywhere in the UI, but it is plain `fetch()` in the page bundles:
reading `/_app/immutable/nodes/*.js` is how I found every endpoint below.

## 2. Authentication

- `POST /login` (form-encoded `email`, `password`) is a SvelteKit *form action*.
  - It needs an `Origin` header equal to the portal origin (SvelteKit's CSRF check).
  - Success: `200 {"type":"redirect","status":303,"location":"/meters"}` plus a `Set-Cookie`.
    Note it is a 200 carrying a "redirect" result, not a real 303. Failure comes back as `{"type":"failure",...}`.
- Session is the cookie `__Secure-better-auth.session_token` (the `better-auth` library),
  `HttpOnly; Secure; SameSite=Lax`, **`Max-Age=3600`**. So: a one-hour session, `Secure` means it only
  round-trips over HTTPS.
- Unauthenticated page requests get `302 -> /login`.
- Sign-out is `POST /api/auth/sign-out`.

I did not let a session actually lapse, so the exact response to an expired cookie on `/portal/*` is
unverified. The client treats both `401` and a redirect to `/login` as "session gone" (see README).

## 3. Endpoints

| Endpoint | Returns |
|---|---|
| `GET /portal/meters/search?q=&page=` | `{data, total, page, pageSize}`; 20/page, 403 meters total. Fields: `meterId, serialNo, make, phaseType, installStatus, dtCode`. `q` matches meter number or serial. |
| `GET /portal/dts?page=` | Distribution transformers (40): `code, name, feederCode, capacityKva`. 20/page. |
| `GET /portal/meters/{id}/geo` | `{data:{latitude, longitude}}`, **as strings**. |
| `GET /portal/meters/{id}/energy` | `{data:[{timestamp, kwh, kvah, voltR}]}`, all strings. Unknown id is `404 {"error":"not_found"}`. |
| `GET /meters/{id}/__data.json` | SvelteKit's server-load payload for the detail page: nameplate + hierarchy. Encoded with SvelteKit's `devalue` format (a flat array with integer back-references), not plain JSON. |
| `GET /portal/keys` | `{data:{signingSecret}}`: a per-user HMAC key (see below). |
| `GET /portal/export?page=` | **Bulk export** of every meter, with hierarchy and geo inline. Needs a signature. |

The UI only exposes the export as an "Export all meters" button on the Transformers page, which is a
good place to hide it.

### The signed export

The page JS does this, and so do we:

1. `GET /portal/keys` -> `signingSecret`.
2. `timestamp = unix seconds`.
3. `signature = hex(HMAC-SHA256(secret, "GET\n/portal/export\n<query string>\n<timestamp>"))`
   where the query string is exactly what is sent (`page=1`).
4. Send `x-timestamp` and `x-signature` headers.

`page=1` returned all 403 meters in one ~230 KB response (`total: 403`), so in practice there is one
page, but the client still pages until `total` is reached. A single export call replaces what would
otherwise be 21 search pages + 403 `geo` + 403 detail-page requests.

Export record:

```json
{"meterId":"J100001","serialNo":"GE84132","make":"L&T","phaseType":"single",
 "installStatus":"Installed","installType":"CT Operated","build":"legacy","dtCode":"DT-002",
 "hierarchy":{"zone":{"name":"Jaipur Zone 2","code":"Z-02"}, "circle":{...}, "division":{...},
              "subdivision":{...}, "substation":{...}, "feeder":{...}, "dt":{...}},
 "geo":{"lat":26.82,"lng":75.90}}
```

It has `installType`, `build` and real numeric `geo`, none of which the list endpoint has.

## 4. Quirks and surprises

1. **Two record "builds".** The export tags each meter `build: "legacy"` (238) or `"v2"` (165). The detail
   page's payload differs by build: legacy returns a `data` list of `{parameterName, parameterValue}`
   pairs; v2 returns a `classData` JSON *string* (`{"installed_meter":{"MeterId":...}}`). Same facts,
   two shapes, so anything scraping detail pages has to handle both. Using the export sidesteps this.
2. **The hierarchy is not a clean tree.**
   - Codes below zone level are **reused under different parents**: `D-01` appears under `C-01`, `C-03`
     and `C-05`. A code is only unique as part of its full path, so the tree must be keyed by path.
   - Some levels have a **blank code or blank name** (22 meters; circle, substation, feeder). The
     detail page agrees with the export (it shows `Circle 6` with no `(C-06)`), so it is the source
     data, not an export artefact. The missing half can be recovered from other meters (the
     name <-> code mapping is one-to-one at every level), which is what this service does, flagged.
   - `v2` meters don't follow the legacy "Zone N -> Circle N" pattern (e.g. J100004: Zone 2, Circle 5).
     I treat that as legitimate data, not an error.
   - For 5 DTs, the feeder reported by the meters differs from the feeder in `/portal/dts`.
3. **Energy data**
   - `kwh`/`kvah` are **cumulative registers**, not per-interval usage. Usage = difference between readings.
   - Timestamps are `DD/MM/YYYY HH:mm` with **no timezone**.
   - The window is **fixed**: 23-30 June 2026. `from`/`to` query params are ignored.
   - **Resolution differs per meter**: in a 54-meter sample, ~80% had 8 daily readings (00:00) and the
     rest had 337 half-hourly readings. It didn't correlate with `build` or status, so don't assume 30 min.
   - Voltage stayed within 220-240 V in everything I sampled. I found no blanks, duplicates or register
     resets in the sample, but the code handles them anyway.
4. **Numbers are strings** in `/geo` and `/energy`.
5. The list's `total` (403) and the export's `total` agree, and all meter ids and serials are unique.
6. `Cache-Control: private, no-store` on page data, so there's no HTTP-level caching to lean on.
