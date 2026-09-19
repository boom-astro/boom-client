# boom-client

Python client for the **main BOOM API**: free-form Mongo queries over the
catalogs, cone searches, aggregation pipelines and cutouts.

```python
from boom import BoomClient, get_object, photometry, plot_lightcurve

client = BoomClient()                      # reads BOOM_API_USER / BOOM_API_PASSWORD

client.catalog_names()
# ['LSST_alerts', 'LSST_alerts_aux', 'ZTF_alerts', 'ZTF_alerts_aux', ...]

obj = get_object(client, "ZTF", "ZTF18abcdefg")
plot_lightcurve(obj)
photometry(obj).head()
```

## Is this the package you want?

BOOM exposes two APIs, with two account systems.

| | `babamul` | `boom-client` (this one) |
|---|---|---|
| API | `/babamul/*`, curated and public | the main API |
| Account | free self-service signup | provisioned by an admin |
| Auth | `BABAMUL_API_TOKEN` (`bbml_…`) | username and password |
| Surface | fixed endpoints: alerts, objects, cutouts, cone search | arbitrary `find`, `count`, `pipeline` on any readable catalog |
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

### cone_search

Accepts a `SkyCoord`, an `(ra, dec)` pair, a list of pairs, or a
`{name: (ra, dec)}` mapping. Results come back keyed the same way.

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

## Objects and photometry

An object's data is split across two collections: `{SURVEY}_alerts` holds
one document per alert, `{SURVEY}_alerts_aux` holds the full photometric
history and the cross-matches. `get_object` joins them into the shape the
`babamul` tools already read.

```python
from boom import get_object, photometry, plot_lightcurve, plot_cutouts

obj = get_object(client, "ZTF", "ZTF18abcdefg")
obj["prv_candidates"], obj["fp_hists"], obj["cross_matches"]
obj["survey_matches"]["lsst"]       # same source in LSST, within 2 arcsec

plot_lightcurve(obj)
plot_cutouts(client, "ZTF", object_id="ZTF18abcdefg", which="brightest")

df = photometry(obj)   # mjd, mag, magerr, band, lim, survey, origin
```

`photometry` merges the alert history, the forced photometry and the
non-detection upper limits, across both surveys when a match was found.

## Caching

Off unless `BOOM_CACHE_DIR` is set. Objects are stored as JSON and
DataFrames as Parquet, keyed by object id, and entries expire after
`BOOM_CACHE_MAX_AGE` (a day by default) because alert catalogs keep
growing and a permanently cached object would hide new photometry.

```python
from boom import Cache

cache = Cache("~/.cache/boom", max_age=3600)
obj = get_object(client, "ZTF", "ZTF18abcdefg", cache=cache)
cache.clear("objects")
```

Raw queries are never cached: caching arbitrary Mongo filters keyed by
their own text is a good way to serve stale results without noticing.

## Things to know before you hammer the API

- **Nothing bounds a query server-side.** The API applies no maximum
  `limit` and no default time budget, so an unselective filter on an
  unindexed field runs as a collection scan against production Mongo.
  This client sends `max_time_ms=60000` by default; pass
  `max_time_ms=None` to remove it, deliberately.
- **`$out` and `$merge` write to the database.** The server passes
  pipeline stages straight to Mongo with no allowlist. This client
  refuses those two stages; everything else is on you.
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
