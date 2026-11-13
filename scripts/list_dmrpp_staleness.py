# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "obstore==0.12.0",
#     "earthaccess==0.19.0",
# ]
# ///
"""Proof of concept: find every data file newer than its DMR++ sidecar by listing S3.

One S3 listing request returns up to 1,000 objects with their sizes and
Last-Modified times. Listing a collection's prefix and pairing each
``<file>`` with ``<file>.dmrpp`` gives a complete answer for that prefix,
with no sampling and no per-file requests.

Run in us-west-2 with an Earthdata login (EARTHDATA_TOKEN, username and
password, or netrc):

    uv run list_dmrpp_staleness.py C2036881712-POCLOUD
    uv run list_dmrpp_staleness.py C2036881712-POCLOUD --up 1 --out stale.csv
    uv run list_dmrpp_staleness.py --prefix s3://podaac-ops-cumulus-protected/SOME_COLLECTION/ --provider POCLOUD

Given a collection concept id, the script asks CMR for one granule, takes
its ``s3://`` link, and lists that file's directory. Layouts differ by DAAC:
if granules are nested by year or day, ``--up N`` climbs N directories so
the listing covers more of the collection. The prefix is printed before
anything is listed; check it is what you meant.

What this PoC is for: finding out whether the DAAC's temporary credentials
allow listing at all (an AccessDenied here is the answer), and how long a
whole collection takes. ``--anonymous`` lists a public bucket instead, to
try the script without credentials.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from typing import Any

CMR = "https://cmr.earthdata.nasa.gov/search/granules.umm_json"
DATA_SUFFIXES = (".nc", ".nc4", ".h5", ".hdf", ".he5", ".hdf5")
SIDECAR = ".dmrpp"
GAPS = (("under 1 hour", 3600), ("under 1 day", 86400), ("under 30 days", 30 * 86400), ("under 1 year", 365 * 86400), ("1 year or more", float("inf")))


def prefix_from_cmr(concept_id: str, up: int) -> str:
    """s3://bucket/dir/ of one of the collection's granules, climbed ``up`` directories."""
    query = urllib.parse.urlencode({"collection_concept_id": concept_id, "page_size": 1, "sort_key": "-start_date"})
    with urllib.request.urlopen(f"{CMR}?{query}", timeout=30) as response:
        items = json.load(response).get("items", [])
    for item in items:
        for related in item["umm"].get("RelatedUrls", []):
            url = related.get("URL", "")
            if url.startswith("s3://") and url.lower().endswith(DATA_SUFFIXES):
                parts = url.split("/")[:-1]
                parts = parts[: max(min(4, len(parts)), len(parts) - up)]  # never climb to the bucket root
                return "/".join(parts) + "/"
    raise SystemExit(f"CMR lists no s3:// HDF or netCDF link for {concept_id}")


def pair(objects: list[dict[str, Any]], tolerance_s: float) -> dict[str, Any]:
    """Match each data file with ``<file>.dmrpp`` and find the files written after their sidecar.

    ``objects`` are listing entries with ``path``, ``last_modified`` and ``size``.
    """
    by_path = {o["path"]: o for o in objects}
    data = [o for o in objects if o["path"].lower().endswith(DATA_SUFFIXES)]
    stale, with_sidecar = [], 0
    for o in data:
        sidecar = by_path.get(o["path"] + SIDECAR)
        if sidecar is None:
            continue
        with_sidecar += 1
        gap = (o["last_modified"] - sidecar["last_modified"]).total_seconds()
        if gap > tolerance_s:
            stale.append({"path": o["path"], "data_last_modified": o["last_modified"].isoformat(), "dmrpp_last_modified": sidecar["last_modified"].isoformat(),
                          "data_newer_by_s": gap, "data_size": o["size"], "dmrpp_size": sidecar["size"]})
    sidecars = [p for p in by_path if p.endswith(SIDECAR)]
    return {
        "objects": len(objects), "data_files": len(data), "with_sidecar": with_sidecar, "without_sidecar": len(data) - with_sidecar,
        "orphan_sidecars": sum(p[: -len(SIDECAR)] not in by_path for p in sidecars), "stale": stale,
    }


def list_prefix(store: Any, prefix: str, max_objects: int) -> tuple[list[dict[str, Any]], int, bool]:
    """(objects, listing requests, whether the listing was cut off at ``max_objects``)."""
    import obstore

    objects: list[dict[str, Any]] = []
    requests = 0
    for page in obstore.list(store, prefix=prefix or None, chunk_size=1000):
        requests += 1
        objects += [{"path": str(m["path"]), "last_modified": m["last_modified"], "size": m["size"]} for m in page]
        if len(objects) >= max_objects:
            return objects, requests, True
    return objects, requests, False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("concept_id", nargs="?", help="a collection concept id, e.g. C2036881712-POCLOUD")
    p.add_argument("--prefix", help="list this s3://bucket/prefix/ instead of asking CMR")
    p.add_argument("--provider", help="CMR provider whose S3 credentials to fetch (default: the concept id's suffix)")
    p.add_argument("--up", type=int, default=0, help="climb this many directories above the sample granule's directory")
    p.add_argument("--tolerance-s", type=float, default=60.0, help="a data file this much newer than its sidecar is stale")
    p.add_argument("--max-objects", type=int, default=200_000, help="stop listing after this many objects")
    p.add_argument("--region", default="us-west-2")
    p.add_argument("--anonymous", action="store_true", help="unsigned requests, for a public bucket")
    p.add_argument("--out", help="write the stale files to this CSV")
    args = p.parse_args(argv)
    if not (args.concept_id or args.prefix):
        p.error("give a collection concept id or --prefix")

    from obstore.store import S3Store

    prefix = args.prefix or prefix_from_cmr(args.concept_id, args.up)
    parsed = urllib.parse.urlparse(prefix)
    bucket, key_prefix = parsed.netloc, parsed.path.lstrip("/")
    print(f"listing s3://{bucket}/{key_prefix}", file=sys.stderr)
    if args.anonymous:
        store = S3Store(bucket, region=args.region, skip_signature=True)
    else:
        import earthaccess

        provider = args.provider or (args.concept_id or "").rpartition("-")[2]
        if not provider:
            p.error("--provider is needed with --prefix")
        earthaccess.login()
        c = earthaccess.get_s3_credentials(provider=provider)
        store = S3Store(bucket, region=args.region, access_key_id=c["accessKeyId"], secret_access_key=c["secretAccessKey"], token=c["sessionToken"])

    start = time.perf_counter()
    objects, requests, truncated = list_prefix(store, key_prefix, args.max_objects)
    seconds = time.perf_counter() - start
    result = pair(objects, args.tolerance_s)
    stale = result["stale"]

    print(f"{result['objects']:,} objects in {requests:,} listing requests, {seconds:.1f} s" + (f" (stopped at --max-objects {args.max_objects:,})" if truncated else ""))
    print(f"{result['data_files']:,} data files: {result['with_sidecar']:,} with a sidecar, {result['without_sidecar']:,} without; {result['orphan_sidecars']:,} sidecars with no data file")
    print(f"{len(stale):,} data files newer than their sidecar by more than {args.tolerance_s:.0f} s")
    if stale:
        buckets: Counter[str] = Counter()
        for row in stale:
            buckets[next(label for label, limit in GAPS if row["data_newer_by_s"] < limit)] += 1
        for label, _ in GAPS:
            if buckets[label]:
                print(f"  {label}: {buckets[label]:,}")
        months = Counter(row["data_last_modified"][:7] for row in stale).most_common(5)
        print("  data files last modified in: " + ", ".join(f"{month} ({n:,})" for month, n in months))
    if args.out:
        with open(args.out, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["bucket", "path", "data_last_modified", "dmrpp_last_modified", "data_newer_by_s", "data_size", "dmrpp_size"])
            writer.writeheader()
            writer.writerows({"bucket": bucket, **row} for row in sorted(stale, key=lambda r: -r["data_newer_by_s"]))
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
