# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "virtualizarr==2.8.0",
#     "obstore==0.12.0",
#     "obspec-utils==0.9.0",
#     "h5py==3.16.0",
#     "xarray==2026.9.0",
#     "zarr==3.4.1",
#     "earthaccess==0.19.0",
# ]
# ///
"""Is a DMR++ sidecar still right for the file beside it?

A sidecar lists a byte offset and length for every chunk. This script reads
the same list a second way, from the file's own HDF5 metadata, and compares
the two chunk by chunk. Only metadata is read; no science data moves.

    uv run verify_dmrpp.py stale_granules.csv --out verdicts.csv            # in us-west-2, direct S3
    uv run verify_dmrpp.py stale_granules.csv --transport https             # anywhere, slower
    uv run verify_dmrpp.py https://archive.podaac.earthdata.nasa.gov/.../file.nc --transport https

The CSV is ``results/dmrpp_staleness/stale_granules.csv`` (columns used:
provider, short_name, concept_id, granule_ur, data_url, dmrpp_url,
data_newer_by_s). A bare URL is a data file's; its sidecar is ``<url>.dmrpp``.

Needs an Earthdata login: ``EARTHDATA_TOKEN``, or ``EARTHDATA_USERNAME`` and
``EARTHDATA_PASSWORD``, or a netrc entry.

Verdicts, one row per granule:

- ``match``: every chunk both sides list has the same offset and length.
- ``MISMATCH``: at least one chunk differs. The sidecar points at the wrong bytes.
- ``SIDECAR_PAST_END``: the sidecar references bytes beyond the end of the file.
- ``nothing_to_compare``: no chunked variable appears on both sides.
- ``not_hdf5``: the file is netCDF classic or HDF4; the file-side parser reads
  HDF5 and netCDF-4 only, so this script cannot judge it.
- ``error: ...``: the file or sidecar could not be read.

Variables only one side lists are counted, not failed: a sidecar may inline
small variables and omit chunks that are all fill value.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from typing import Any

CMR = "https://cmr.earthdata.nasa.gov/search/granules.umm_json"
FIELDS = ["verdict", "provider", "short_name", "granule_ur", "data_newer_by_days", "variables_compared", "chunks_compared",
          "chunks_mismatched", "variables_mismatched", "only_in_sidecar", "only_in_file", "file_size", "sidecar_max_byte",
          "first_mismatch", "data_url"]


def manifests(group: Any, prefix: str = "") -> dict[str, dict[str, dict[str, Any]]]:
    """{variable path: {chunk key: {path, offset, length}}} for every array under a ManifestGroup."""
    out = {f"{prefix}/{name}": array.manifest.dict() for name, array in group.arrays.items()}
    for name, child in group.groups.items():
        out.update(manifests(child, f"{prefix}/{name}"))
    return out


def compare(sidecar: dict[str, dict[str, dict[str, Any]]], file: dict[str, dict[str, dict[str, Any]]], file_size: int | None) -> dict[str, Any]:
    """The verdict for one granule from its two chunk indexes."""
    shared = sorted(set(sidecar) & set(file))
    chunks = mismatched = 0
    bad_variables, first = [], None
    for name in shared:
        a, b = sidecar[name], file[name]
        differing = [k for k in sorted(set(a) & set(b)) if (a[k]["offset"], a[k]["length"]) != (b[k]["offset"], b[k]["length"])]
        chunks += len(set(a) & set(b))
        mismatched += len(differing)
        if differing:
            bad_variables.append(name)
            k = differing[0]
            first = first or f"{name}[{k}]: sidecar {a[k]['offset']}+{a[k]['length']}, file {b[k]['offset']}+{b[k]['length']}"
    max_byte = max((c["offset"] + c["length"] for m in sidecar.values() for c in m.values()), default=0)
    if file_size is not None and max_byte > file_size:
        verdict = "SIDECAR_PAST_END"
    elif mismatched:
        verdict = "MISMATCH"
    elif chunks == 0:
        verdict = "nothing_to_compare"
    else:
        verdict = "match"
    return {
        "verdict": verdict, "variables_compared": len(shared), "chunks_compared": chunks, "chunks_mismatched": mismatched,
        "variables_mismatched": len(bad_variables), "only_in_sidecar": len(set(sidecar) - set(file)), "only_in_file": len(set(file) - set(sidecar)),
        "file_size": file_size, "sidecar_max_byte": max_byte, "first_mismatch": first,
    }


def pick(rows: list[dict[str, str]], per_collection: int, min_gap_days: float, limit: int | None, seed: int = 0) -> list[dict[str, str]]:
    """Up to ``per_collection`` granules from each collection, largest gaps first across collections."""
    by: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if float(row.get("data_newer_by_s") or 0) >= min_gap_days * 86400:
            by[row["concept_id"]].append(row)
    rng = random.Random(seed)
    chosen = []
    for group in sorted(by.values(), key=lambda g: -max(float(r.get("data_newer_by_s") or 0) for r in g)):
        chosen += rng.sample(group, min(per_collection, len(group)))
    return chosen[:limit] if limit else chosen


def s3_url(row: dict[str, str]) -> str:
    """The granule's s3:// link from CMR; the HTTPS URL's first path segment as the bucket if CMR has none."""
    name = row["data_url"].rsplit("/", 1)[-1]
    if row.get("granule_ur") and row.get("concept_id"):
        query = urllib.parse.urlencode({"collection_concept_id": row["concept_id"], "granule_ur": row["granule_ur"]})
        with urllib.request.urlopen(f"{CMR}?{query}", timeout=30) as response:
            for item in json.load(response).get("items", []):
                for related in item["umm"].get("RelatedUrls", []):
                    if related.get("URL", "").startswith("s3://") and related["URL"].endswith(name):
                        return related["URL"]
    parsed = urllib.parse.urlparse(row["data_url"])
    return f"s3:/{parsed.path}"


class Access:
    """Object stores per bucket or host, built once."""

    def __init__(self, transport: str) -> None:
        import earthaccess

        self.transport = transport
        self.auth = earthaccess.login()
        self._stores: dict[str, Any] = {}
        self._credentials: dict[str, dict[str, str]] = {}

    def locate(self, row: dict[str, str]) -> tuple[str, Any]:
        """(the data file's URL on this transport, the store that serves it)."""
        from obstore.store import HTTPStore, S3Store

        if self.transport == "https":
            url = row["data_url"]
            root = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(url))
            if root not in self._stores:
                token = self.auth.token["access_token"]
                self._stores[root] = HTTPStore(root, client_options={"default_headers": {"authorization": f"Bearer {token}"}})
            return url, (root, self._stores[root])
        import earthaccess

        url = row["data_url"] if row["data_url"].startswith("s3://") else s3_url(row)
        bucket = urllib.parse.urlparse(url).netloc
        if bucket not in self._stores:
            provider = row.get("provider") or ""
            if provider not in self._credentials:
                self._credentials[provider] = earthaccess.get_s3_credentials(provider=provider)
            c = self._credentials[provider]
            self._stores[bucket] = S3Store(bucket, region="us-west-2", access_key_id=c["accessKeyId"],
                                           secret_access_key=c["secretAccessKey"], token=c["sessionToken"])
        return url, (f"s3://{bucket}", self._stores[bucket])


def verify(row: dict[str, str], access: Access) -> dict[str, Any]:
    import obstore
    from obspec_utils.registry import ObjectStoreRegistry
    from virtualizarr.parsers import DMRPPParser, HDFParser

    out: dict[str, Any] = {
        "provider": row.get("provider"), "short_name": row.get("short_name"), "granule_ur": row.get("granule_ur"), "data_url": row["data_url"],
        "data_newer_by_days": round(float(row["data_newer_by_s"]) / 86400, 2) if row.get("data_newer_by_s") else None,
    }
    try:
        url, (root, store) = access.locate(row)
        registry = ObjectStoreRegistry({root: store})
        size = int(obstore.head(store, url[len(root):].lstrip("/"))["size"])
        from_sidecar = manifests(DMRPPParser()(url + ".dmrpp", registry)._group)
        from_file = manifests(HDFParser()(url, registry)._group)
        out.update(compare(from_sidecar, from_file, size))
    except Exception as error:  # noqa: BLE001 -- one unreadable granule is a row, not the end of the run
        if "file signature not found" in str(error):
            out["verdict"] = "not_hdf5"  # netCDF classic or HDF4: the file-side parser cannot index it
        else:
            out["verdict"] = f"error: {type(error).__name__}: {str(error)[:200]}"
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("inputs", nargs="+", help="stale_granules.csv, or data file URLs")
    p.add_argument("--transport", choices=("s3", "https"), default="s3", help="s3: direct, from us-west-2 (default); https: the DAAC endpoint, from anywhere")
    p.add_argument("--per-collection", type=int, default=2, help="granules to check per collection (CSV input)")
    p.add_argument("--min-gap-days", type=float, default=0.0, help="only granules whose data file is at least this much newer than its sidecar")
    p.add_argument("--limit", type=int, help="stop after this many granules")
    p.add_argument("--out", help="write verdicts to this CSV as well as the screen")
    args = p.parse_args(argv)

    rows: list[dict[str, str]] = []
    for item in args.inputs:
        if item.endswith(".csv"):
            with open(item, newline="") as f:
                rows += pick(list(csv.DictReader(f)), args.per_collection, args.min_gap_days, None)
        else:
            rows.append({"data_url": item, "provider": os.environ.get("CMR_PROVIDER", "")})
    rows = rows[: args.limit] if args.limit else rows
    print(f"{len(rows)} granules to verify over {args.transport}", file=sys.stderr)
    access = Access(args.transport)
    sink = open(args.out, "w", newline="") if args.out else None  # noqa: SIM115 -- closed below; rows are flushed as they arrive
    writer = csv.DictWriter(sink, fieldnames=FIELDS, extrasaction="ignore") if sink else None
    if writer:
        writer.writeheader()
    tally: dict[str, int] = defaultdict(int)
    for index, row in enumerate(rows, 1):
        result = verify(row, access)
        tally[result["verdict"].split(":")[0]] += 1
        detail = f"{result.get('chunks_mismatched')} of {result.get('chunks_compared')} chunks differ" if "chunks_compared" in result else ""
        print(f"[{index}/{len(rows)}] {result['verdict']:<18} {result.get('short_name') or ''} {row['data_url'].rsplit('/', 1)[-1]} {detail}", flush=True)
        if result.get("first_mismatch"):
            print(f"      {result['first_mismatch']}", flush=True)
        if writer and sink:
            writer.writerow(result)
            sink.flush()
    if sink:
        sink.close()
    print("\n" + ", ".join(f"{k}: {v}" for k, v in sorted(tally.items())))
    return 1 if tally.get("MISMATCH") or tally.get("SIDECAR_PAST_END") else 0


if __name__ == "__main__":
    sys.exit(main())
