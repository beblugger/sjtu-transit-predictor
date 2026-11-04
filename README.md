# SJTU Transit Predictor

Backend-only predictor for aircraft and satellite transits across the Sun or
Moon as seen anywhere inside Shanghai Jiao Tong University's Minhang campus.
It returns a JSON event list and a server-rendered HTML page.

## Capabilities

- WGS84/CGCS2000 geodetic coordinates transformed through ECEF into a local
  campus ENU frame. No GCJ-02 shift is applied.
- The official SJTU Minhang campus polygon, a 100 m observation grid, and 25 m
  local refinement around candidate events.
- CelesTrak active OMM/TLE ingestion and SGP4 propagation for a rolling 96-hour
  window. The primary is checked at most once every two hours, with a community
  OMM mirror and a persistent last-valid catalog as fallbacks. Six-digit catalog
  numbers are supported through OMM.
- Sun/Moon astrometry from Skyfield DE421 when that optional kernel is
  available, with an offline Meeus analytical fallback. Events are refined
  below one second rather than sampled only once per hour.
- Physical angular diameter for satellites with audited dimensions:
  `theta = 2 atan(D / (2 range))`. Unknown sizes are reported as unknown and
  only central-line crossings are accepted.
- Aircraft trajectory prediction using robust local cubic, constant-velocity,
  and coordinated-turn models over a ten-minute horizon.
- Open-Meteo deterministic and ECMWF/GFS ensemble forecasts with a composite
  `P0/P3/P12` score, model agreement, run stability, and lead-time thresholds.
- An official VariFlight DataWorks area-provider adapter. It remains explicitly
  disabled until trial credentials are configured.

## Setup

Python 3.10 or newer is required.

```bash
python3 -m pip install -e .
cp .env.example .env
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

For an isolated environment use standard Python `venv` and install `.[dev]`.
No uv or Node.js is required for the backend. Python 3.14 is supported.

DE421 is optional. Put `de421.bsp` at
`~/.cache/sjtu-transit/de421.bsp` to use Skyfield's JPL ephemeris; otherwise the
service automatically uses its built-in analytical Sun/Moon model. The first
orbit refresh loads the active satellite catalog and can take a little time.
CelesTrak's "not updated" response is handled as an unchanged catalog, with
the public `satvisorcom/satvisor-data` OMM mirror as an outage fallback.
An unchanged response does not reset the original download timestamp. A six-hour
warning threshold never excludes a satellite solely because its epoch is old.
If a mirror lags behind the last-valid snapshot, newer cached elements are kept;
fallback merging is reported as `community-mirror+cache`. A successful primary
snapshot replaces catalog membership normally.

Open:

- API: `http://127.0.0.1:8000/api/v1/events?type=satellite&hours=96`
- OMM catalog: `http://127.0.0.1:8000/api/v1/orbits`
- Legacy TLE: `http://127.0.0.1:8000/api/v1/orbits?format=tle`
- Weather snapshot: `http://127.0.0.1:8000/api/v1/weather/snapshot`
- Weather: `http://127.0.0.1:8000/api/v1/weather/status`
- Health: `http://127.0.0.1:8000/api/v1/health`
- HTML: `http://127.0.0.1:8000/events.html`
- OpenAPI: `http://127.0.0.1:8000/docs`

Each satellite event includes the physical span, nominal angular diameter,
minimum/maximum orientation-dependent angular diameter, size-source URL, and
size confidence. The nominal value uses the maximum documented span for
conservative transit ingress/egress detection. The HTML page renders the
official campus polygon and marks the best observation points returned by the
campus-wide scan.

## Batch/static backend

```bash
python3 -m app.export --database .state/transit.db --output dist/data --hours 96
```

This is a finite batch job: it does not start FastAPI, background polling, or
the aircraft module. It exports every geometric Sun/Moon transit, including
events with poor, missing, or stale weather. The browser can filter the event
window and weather separately. The API likewise defaults to all events; pass
`announced_only=true` to request weather-approved events only.

Outputs:

- `events.json`: all events and the fixed UTC prediction coverage.
- `orbits.json`: complete OMM catalog for consumers, including six-digit IDs.
- `orbits.tle`: compatibility subset with IDs up to 99999; the manifest records
  how many objects were omitted. OMM must be used for complete coverage.
- `weather.json`: cached deterministic/ensemble series with original fetch times.
- `campus.geojson`: the observation area.
- `manifest.json`: source, original fetch time, last check, hashes, warnings,
  and event/catalog counts. It is committed last; consumers should verify hashes
  and retry if files changed while loading.

Retain `.state/transit.db` between jobs. It stores orbit snapshots, primary
request attempts (including failures), weather, and six-hour geometry chunks.
Unchanged geometry chunks are reused; changed orbital data, numerical code,
dependencies, campus, sizes, or calculation settings invalidate them. New tail
chunks are computed as the 96-hour window advances. Prediction cache entries
older than seven days are removed. Use `--offline` to reproduce an export from
stored data without fetching orbit/weather providers. With no usable catalog,
the job fails before replacing an existing export.

Weather requests cover six forecast days plus one past day for the ±12-hour
score. A missing full window yields `weather_window_not_covered`, rather than
using the last available hour for a later date. The snapshot includes location,
member forecasts, and fetch timestamps so a future frontend can optionally
refresh and re-score it. Free Open-Meteo service is for non-commercial use and
requires attribution.

Use DE421 for production transit calculations. Download the JPL kernel once:

```bash
mkdir -p ~/.cache/sjtu-transit
curl --fail --location https://ssd.jpl.nasa.gov/ftp/eph/planets/bsp/de421.bsp \
  --output ~/.cache/sjtu-transit/de421.bsp
```

Without it the analytical fallback remains available, but the export explicitly
warns `analytical_ephemeris_lower_accuracy`. Numerical subsecond refinement does
not imply subsecond physical prediction accuracy: event uncertainty grows with
distance from the orbit epoch, and maneuvers may invalidate an extrapolation.
Events include `orbit_epoch` and `orbit_age_at_event_hours`. Satellite sunlight
and Earth-shadow status are not filters for Sun/Moon transits.

The coarse search uses 60-second finite trajectory arcs, then an array-based
100 m campus screen and 25 m local refinement. A short-window cubic interpolation
of Sun/Moon apparent positions accelerates observation-point screening; final
event and contact times use the full ephemeris calculation. Minima at search
boundaries are rejected, and contact roots can extend up to one hour on either
side so slow transits are not truncated to the coarse search window.

To independently audit exported positions and contact boundaries using
Skyfield's satellite/topocentric implementation:

```bash
python3 scripts/validate_export.py --data dist/data
```

The audit verifies snapshot hashes, campus membership, coverage, and geometric
agreement to one arcsecond. It does not establish real-world orbit accuracy or
prove that the coarse candidate search is exhaustive.

Local validation on 2026-11-04 (Python 3.14): 33 tests passed. A live CelesTrak
catalog of 16,875 objects produced 1,166 geometric events over 96 hours using
DE421, including 60 events with six-digit satellite IDs. Cold export took
343 seconds on the development machine; a second offline export reused all
17 chunks in 4.5 seconds. The independent audit checked every event with no
violations; maximum minimum-separation difference was 0.67 arcsecond and maximum
contact-boundary residual was 0.77 arcsecond. These are numerical consistency
checks, not estimates of orbital prediction uncertainty. GitHub runner timing
has not yet been measured.

## Hosted backend

The public repository is https://github.com/beblugger/sjtu-transit-predictor.
Static data is served at https://beblugger.github.io/sjtu-transit-predictor/data/manifest.json.
No continuously running API server or aircraft provider is deployed.

`.github/workflows/backend.yml` builds on main pushes, manual dispatch, and
at minute 23 of every even UTC hour (Beijing 00:23, 02:23, etc.). Each run
installs Python 3.14, tests the backend, computes 96 hours, independently audits
all exported events, and deploys the complete snapshot through GitHub Pages.
The previous successful Pages deployment survives a failed build or audit.
The root page only lists data links; the prediction frontend is not deployed.

Only the repository-provided `GITHUB_TOKEN` is used. There are no custom secrets,
paid runners, paid APIs, or external cron services. Workflow actions are pinned
to commit hashes and overlapping builds are serialized. DE421 is verified by
SHA256 before use. Contents write permission is limited to the build job;
Pages and OIDC write permissions are limited to deployment.

Actions cache accelerates geometry and ephemeris reuse. A separate `state`
branch stores a compact SQLite snapshot of orbit/weather inputs and original
fetch/check times. It deliberately omits geometry and aircraft samples. On a
cache miss the workflow restores these durable inputs before fetching providers.
Each successful build replaces **only the dedicated state branch** with a new
root commit, leaving only the latest binary snapshot reachable. Main history
is never rewritten. The state is public scientific data, with no credentials.
This avoids retaining a growing Git history of large databases; GitHub controls
when unreachable objects are garbage-collected. State updates contain actual
new export metadata rather than empty keepalive commits.

Scheduled Actions can run late or be dropped, and GitHub can disable inactive
public schedules after 60 days. Real state updates provide repository activity,
but GitHub does not guarantee permanent operation. Monitor the Actions page and
`manifest.generated_at`; a manual **Run workflow** can recover after an outage.
Backend data includes original source timestamps and stale-data warnings.
Actions cache eviction affects speed, not the durable input fallback.

Official references: [Pages custom workflows](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages),
[scheduled workflow limitations](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule),
and [cache limits](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching#usage-limits-and-eviction-policy).

## Aircraft ADS-B source

The public OpenSky, adsb.lol, and AirLabs demo feeds currently return no
aircraft in the Shanghai bounding box. FlightADSB visibly covers Shanghai but
does not expose a public API and its terms prohibit automated collection.

The implemented provider therefore targets the official VariFlight DataWorks
Radar ADS-B V2 area query. Request a 14-day trial and set:

```dotenv
AIRCRAFT_ADS_PROVIDER=variflight
AIRCRAFT_VARIFLIGHT_APP_ID=...
AIRCRAFT_VARIFLIGHT_APP_SECURITY=...
```

Without these values the satellite and weather services remain available while
the aircraft module reports `aircraft_provider_unavailable`.

## Weather policy

For each Sun/Moon transit candidate the service computes:

- `P0`: ensemble fraction clear at the event hour.
- `P3`: clear fraction across the event plus/minus three hours.
- `P12`: clear fraction across the event plus/minus twelve hours.
- Each model score is `(P0 * P3 * P12) ** (1/3)`.
- ECMWF and GFS scores are averaged. A model gap above 0.20 or a run-to-run
  score change above 0.20 suppresses the event.
- Announcement thresholds are 0.70, 0.78, 0.85, and 0.90 for lead-time bands
  ending at 2, 12, 24, and 48 hours.

All events within the 96-hour window use this policy. Missing or stale weather
does not create a false "clear" result.

## Tests

```bash
pytest
```

The test suite does not require external network access.
