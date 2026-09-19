"""End-to-end tour of the BOOM client.

Run it with credentials in the environment or in a .env file:

    BOOM_API_USER=... BOOM_API_PASSWORD=... python examples/quickstart.py
"""

from __future__ import annotations

import astropy.units as u
from astropy.coordinates import SkyCoord

from boom import BoomClient, Cache, get_object, photometry
from boom.exceptions import APINotFoundError


def main() -> None:
    with BoomClient() as client:
        print(f"connected to {client.config.url}\n")

        print("catalogs you can read:")
        for name in client.catalog_names():
            print(f"  {name}")

        print("\nZTF_alerts size:")
        print(f"  {client.estimated_count('ZTF_alerts'):,} alerts (estimate)")

        print("\nindexes on ZTF_alerts:")
        for index in client.catalog_indexes("ZTF_alerts"):
            print(f"  {index['name']}: {dict(index['key'])}")

        print("\none random alert, to see the schema:")
        sample = client.catalog_sample("ZTF_alerts", size=1)[0]
        print(f"  {sorted(sample)}")

        object_id = sample["objectId"]

        print(f"\nbrightest recent alerts for {object_id}:")
        for alert in client.find(
            "ZTF_alerts",
            {"objectId": object_id},
            projection={"candidate.jd": 1, "candidate.magpsf": 1},
            sort={"candidate.magpsf": 1},
            limit=3,
        ):
            candidate = alert["candidate"]
            print(f"  jd={candidate['jd']} mag={candidate['magpsf']}")

        print(f"\nfull object for {object_id}:")
        obj = get_object(
            client, "ZTF", object_id, cache=Cache("~/.cache/boom")
        )
        table = photometry(obj)
        print(f"  {len(table)} photometry points")
        print(table.head().to_string(index=False))

        radec = obj["candidate"]["ra"], obj["candidate"]["dec"]
        print(f"\nneighbours within 30 arcsec of {radec}:")
        results = client.cone_search(
            "ZTF_alerts_aux",
            SkyCoord(*radec, unit="deg"),
            30 * u.arcsec,
            projection={"_id": 1},
        )
        for neighbour in results["0"]:
            print(f"  {neighbour['_id']}")

        print("\nbusiest objects of the last night:")
        for row in client.pipeline(
            "ZTF_alerts",
            [
                {"$match": {"candidate.jd": {"$gt": 2460000.5}}},
                {"$group": {"_id": "$objectId", "n": {"$sum": 1}}},
                {"$sort": {"n": -1}},
                {"$limit": 5},
            ],
        ):
            print(f"  {row['_id']}: {row['n']} alerts")


if __name__ == "__main__":
    try:
        main()
    except APINotFoundError as error:
        raise SystemExit(f"not found: {error}") from error
