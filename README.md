# boom-client

Python client for the **main BOOM API**: free-form Mongo queries over the
catalogs, cone searches, aggregation pipelines and cutouts.

```python
from boom import BoomClient, get_source

client = BoomClient()                      # reads BOOM_API_USER / BOOM_API_PASSWORD

client.catalog_names()
# ['LSST_alerts', 'LSST_alerts_aux', 'ZTF_alerts', 'ZTF_alerts_aux', ...]

src = get_source(client, "ZTF", "ZTF18abcdefg")
src.ndethist, src.peak_mag, src.age, src.redshift
src.plot_lightcurve()
src.get_detections()
```

## Is this the package you want?

BOOM exposes two APIs, with two account systems.

| | `babamul` | `boom-client` (this one) |
|---|---|---|
| API | `/babamul/*`, curated and public | the main API |
| Account | free self-service signup | provisioned by an admin |
| Auth | `BABAMUL_API_TOKEN` (`bbml_…`) | username and password |
| Surface | fixed endpoints: alerts, objects, cutouts, cone search | arbitrary `find`, `count`, `pipeline` on any readable catalog |
| Analysis | alert models and plotting | the same, plus typed sources, cleaned photometry and a disk cache |
| Real-time | yes, Kafka streams | no, pull only |

If you signed up on the Babamul website, you want
[`babamul`](https://github.com/boom-astro/babamul). A Babamul token is
rejected by the main API, and this client says so rather than returning a
bare 401.

This package **depends on** `babamul` and reuses its alert models, light
curve merging and plotting, so the two behave the same where they overlap.

## Install

```bash
pip install boom-client
```

## Configuration

| Variable | Meaning |
|---|---|
| `BOOM_API_USER` | BOOM account username |
| `BOOM_API_PASSWORD` | BOOM account password |
| `BOOM_ENV` | `production` (Caltech, default), `backup` (UMN) or `local` |
| `BOOM_API_URL` | Full base URL override, wins over `BOOM_ENV` |
| `BOOM_CACHE_DIR` | Enables the on-disk cache. Unset means no caching |
| `BOOM_CACHE_MAX_AGE` | Cache entry lifetime in seconds (default 86400) |
| `BOOM_NO_VERSION_CHECK` | Set to `1` to stop asking PyPI, once a day on import, whether a newer release is out |

Credentials can also be passed directly:

```python
client = BoomClient(username="alice", password="...", url="http://localhost:4000")
```

The client logs in lazily on the first request and renews its token when
the API rejects it, so one instance survives a long notebook session.

## Querying

### find

```python
alerts = client.find(
    "ZTF_alerts",
    {"objectId": "ZTF18abcdefg"},
    projection={"candidate.jd": 1, "candidate.magpsf": 1},
    sort={"candidate.jd": -1},
    limit=10,
)

alert = client.find_one("ZTF_alerts", {"_id": 2695378462115015007})
```

For anything larger than a page, use `iter_find`, which pages with
skip/limit and sorts on `_id` so pages neither overlap nor skip rows:

```python
for alert in client.iter_find("ZTF_alerts", {"candidate.drb": {"$gt": 0.9}}, limit=50_000):
    ...
```

### count

```python
client.count("ZTF_alerts", {"objectId": "ZTF18abcdefg"})  # exact, uses the index
client.estimated_count("ZTF_alerts")                      # metadata only, instant
```

### cone_search and near_search

Two different questions. `cone_search` answers "everything inside this
radius", in no particular order, for many positions at once. `near_search`
answers "what is closest", one position at a time, and tags each result
with a `distance_arcsec`. Cross-matching wants the second.

```python
client.near_search("NED", 189.1, 28.4, radius_arcsec=10)
# [{'_id': 'NGC1234', 'z': 0.031, ..., 'distance_arcsec': 2.3}, ...]
```

`cone_search` accepts a `SkyCoord`, an `(ra, dec)` pair, a list of pairs,
or a `{name: (ra, dec)}` mapping. Results come back keyed the same way.

```python
import astropy.units as u
from astropy.coordinates import SkyCoord

client.cone_search("ZTF_alerts_aux", SkyCoord("12h34m56s +30d00m00s"), 5 * u.arcsec)

client.cone_search(
    "LSST_alerts_aux",
    {"sn2024abc": (189.1, 28.4), "sn2024def": (201.7, -5.2)},
    3,
    "arcsec",
)
```

### pipeline

```python
client.pipeline("ZTF_alerts", [
    {"$match": {"candidate.jd": {"$gt": 2460000.5}}},
    {"$group": {"_id": "$objectId", "n": {"$sum": 1}}},
    {"$sort": {"n": -1}},
    {"$limit": 20},
])
```

### Inspecting catalogs

```python
client.catalogs(details=True)              # with storage statistics
client.catalog_indexes("ZTF_alerts")       # check before a big find
client.catalog_sample("ZTF_alerts", 3)     # random documents, to see the schema
```

## Objects, sources and photometry

An object's data is split across two collections: `{SURVEY}_alerts` holds
one document per alert, `{SURVEY}_alerts_aux` holds the full photometric
history and the cross-matches. There are two levels on top of that join.

### get_object: the raw document

Merges the two, in the shape the `babamul` tools already read, and finds
the counterpart in the other survey, by recorded alias where there is one
and by position otherwise.

```python
from boom import get_object, plot_lightcurve, plot_cutouts

obj = get_object(client, "ZTF", "ZTF18abcdefg")
obj["prv_candidates"], obj["fp_hists"], obj["cross_matches"]
obj["survey_matches"]["lsst"]

plot_lightcurve(obj)
plot_cutouts(client, "ZTF", object_id="ZTF18abcdefg", which="brightest")
```

Only the fields this package reads are requested, since an untrimmed aux
document for an old ZTF object runs to megabytes. Pass `trim=False` for
everything, or use `boom.projections` to build your own.

### get_source: the typed source

```python
from boom import get_source

src = get_source(client, "ZTF", "ZTF18abcdefg")

src.objectid, src.ztfid, src.lsstid       # identity across surveys
src.ra, src.dec, src.jd
src.redshift, src.redshift_origin         # host, from the cross-matches
src.offset, src.host_origin               # separation to that host

src.ndethist, src.ndetfilters, src.filters
src.jdstarthist, src.jdendhist, src.age
src.peak_mag, src.median_ra, src.median_dec

src.get_photometry()                      # everything
src.get_detections(min_snr=5.0)           # real, positive detections only
src.plot_lightcurve()
src.to_json(), Source.from_json(blob)
```

### The photometry table

Both `photometry(obj)` and `Source.get_photometry()` return the same
schema, one row per measurement:

| column | meaning |
|---|---|
| `jd`, `mjd` | epoch |
| `band` | filter |
| `magpsf`, `sigmapsf` | PSF magnitude and its error |
| `diffmaglim` | limiting magnitude of the visit |
| `snr` | signal to noise |
| `ra`, `dec` | measured position |
| `psf_flux`, `psf_flux_err` | difference flux |
| `isdiffpos` | the difference is positive, i.e. the source brightened |
| `det_type` | `alert`, `fp` (forced photometry) or `ul` (upper limit) |
| `survey` | which survey produced the row |

The three histories overlap in time, so epochs already reported as alerts
are dropped from the forced photometry and the upper limits. Rows the
survey flags as unusable are dropped too: low real-bogus score, too many
bad pixels or a shallow limit for ZTF, low reliability and the dipole,
centroid, pixel and glint flags for LSST. Pass `quality_cuts=False` to see
everything the pipeline stored, and read `boom.photometry.CUTS` for the
exact thresholds.

A negative difference is not a cut. LSST's `isNegative` and ZTF's
`isdiffpos` record that the source faded relative to the reference, which
is a measurement, not a defect: those rows stay in the table, flagged by
`isdiffpos`, and it is `get_detections()` that leaves them out. Cutting
them earlier would delete most of a variable source's light curve.

## Caching

Off unless `BOOM_CACHE_DIR` is set. Entries expire after
`BOOM_CACHE_MAX_AGE` (a day by default) because alert catalogs keep
growing and a permanently cached object would hide new photometry.

```python
from boom import Cache, Source

cache = Cache("~/.cache/boom", max_age=3600)
src = get_source(client, "ZTF", "ZTF18abcdefg", cache=cache)
Source.load(cache, "ZTF18abcdefg")
cache.clear("sources")
```

Layout under the cache directory, all keyed by object id:

| namespace | format | holds |
|---|---|---|
| `objects/` | JSON | merged alert and history documents |
| `sources/` | Parquet | one row of source metadata and summaries |
| `photometry/` | Parquet | the measurement table |
| `crossmatches/` | JSON | the cross-match documents |

Writes go through a temporary file, so an interrupted run leaves no half
entry, and a corrupt or stale file reads as a miss rather than an error.

A cache built one object at a time reads back as a sample:

```python
cache.load_frames("sources")       # one row per cached source, with summaries
cache.load_frames("photometry")    # every measurement of every cached source
cache.keys("sources")              # what is on disk
```

Raw queries are never cached: caching arbitrary Mongo filters keyed by
their own text is a good way to serve stale results without noticing.

## Things to know before you hammer the API

- **The API bounds every query.** A query without its own `max_time_ms`
  stops after 30 seconds; this client sends 60000 by default. An answer
  over 256 MB is refused with a 400 that asks for a `limit`, paging or
  fewer projected fields. A cone search takes at most 10000 positions per
  request, so this client splits larger ones. While 8 large queries are
  running, the next one waits up to a minute and then gets a 503, which
  this client retries.
- **`$out` and `$merge` write to the database.** The server passes
  pipeline stages straight to Mongo with no allowlist. This client
  refuses those two stages; everything else is on you.
- **A malformed query comes back as 500, not 400.** The server hands the
  filter to Mongo and wraps the resulting error in a 500, so this client
  does not retry 500 at all: retrying a syntax error would cost five
  round-trips against production Mongo and delay the message by twenty
  seconds without ever succeeding. Transient 502, 503, 504 and 429 are
  still retried.
- **404 also means "no access".** Catalogs beginning with `watchlist_`
  are gated per account, and the API answers 404 rather than 403 for
  them, so `APINotFoundError` can mean either.
- **Catalog schemas are internal.** Field names follow the Rust
  ingestion pipeline and change without a deprecation window. Pin what
  matters in your own code rather than assuming stability.

## Development

```bash
uv sync
make test          # pytest, fully offline against a mock transport
make check-types   # mypy --strict
make format        # ruff via prek
```

The test suite never touches the network: it serves a fake API through
`httpx.MockTransport`, so no credentials are needed to run it.

## License

MIT
