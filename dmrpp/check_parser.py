# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "virtualizarr @ git+https://github.com/maxj-labs/VirtualiZarr@fix/dmrpp-parser-sidecars",
#     "obstore==0.12.0",
#     "obspec-utils==0.9.0",
#     "h5py==3.16.0",
#     "zarr==3.4.1",
#     "earthaccess==0.19.0",
# ]
# ///
"""Do values read through a DMR++ sidecar equal the file's own, with the fixed parser?

Temporary, and the one file here that is run on its own (``uv run`` reads the
dependencies above): it needs the fork's VirtualiZarr, which cannot share an
environment with the release the rest of this repository is locked to. Delete
it once the parser fixes are released.

This checks the VirtualiZarr DMR++ parser (the ``fix/dmrpp-parser-sidecars``
branch of maxj-labs/VirtualiZarr), not the sidecars and not Hyrax. For each
granule it builds a virtual store from the sidecar, reads variables through
it, and compares them with a direct h5py read of the same file.

    uv run dmrpp/check_parser.py --provider POCLOUD s3://podaac-ops-cumulus-protected/ECCO_L4_OBP_LLC0090GRID_DAILY_V4R4B/OCEAN_BOTTOM_PRESSURE_day_mean_1997-06-12_ECCO_V4r4b_native_llc0090.nc

Run it in us-west-2: every chunk is one request, and through a DAAC's HTTPS
endpoint each of those is a redirect and a signed URL. To keep it cheap in
any case, a variable is only read if it has at most ``--max-chunks`` chunks
and ``--max-mb`` of data; larger ones are listed as not compared.

Per granule it prints the variables the parser skipped (with the reason),
then how many variables were equal, different, errored or not compared.
Exit code 1 if any variable is different or errors.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import urllib.parse
import warnings
from typing import Any


def store_for(url: str, provider: str, token: str | None, cache: dict[str, Any]) -> tuple[str, Any]:
    """(root URL, object store) for an s3:// or https:// granule URL."""
    from obstore.store import HTTPStore, S3Store

    parsed = urllib.parse.urlparse(url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    if root not in cache:
        if parsed.scheme == "s3":
            import earthaccess

            earthaccess.login()
            c = earthaccess.get_s3_credentials(provider=provider)
            cache[root] = S3Store(parsed.netloc, region="us-west-2", access_key_id=c["accessKeyId"], secret_access_key=c["secretAccessKey"], token=c["sessionToken"])
        else:
            cache[root] = HTTPStore(root, client_options={"default_headers": {"authorization": f"Bearer {token}"}})
    return root, cache[root]


def comparable(array: Any, max_chunks: int, max_bytes: int) -> str | None:
    """None if the variable is cheap enough to read; else why not."""
    metadata = array.metadata
    chunks = math.prod(math.ceil(n / c) for n, c in zip(metadata.shape, metadata.chunk_grid.chunk_shape, strict=True)) if metadata.shape else 1
    nbytes = math.prod(metadata.shape) * metadata.data_type.to_native_dtype().itemsize
    if chunks > max_chunks:
        return f"{chunks:,} chunks"
    if nbytes > max_bytes:
        return f"{nbytes / 1e6:,.0f} MB"
    return None


def check(url: str, store_root: tuple[str, Any], max_chunks: int, max_bytes: int) -> dict[str, list[str]]:
    import h5py
    import numpy as np
    import zarr
    from obspec_utils.readers import BlockStoreReader
    from obspec_utils.registry import ObjectStoreRegistry
    from virtualizarr.parsers import DMRPPParser

    root, store = store_root
    key = url[len(root):].lstrip("/")
    out: dict[str, list[str]] = {"skipped by the parser": [], "equal": [], "DIFFERENT": [], "error": [], "not compared": []}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        manifest_store = DMRPPParser()(url + ".dmrpp", ObjectStoreRegistry({root: store}))
    for warning in caught:
        text = str(warning.message)
        if "could not be parsed" in text:
            name = text.split("variable ", 1)[1].split(" that", 1)[0]
            reason = text.rsplit("(", 1)[-1].rstrip(")") if "(" in text else ""
            out["skipped by the parser"].append(f"{name} ({reason[:80]})")
    group = zarr.open_group(manifest_store, mode="r", zarr_format=3)
    with BlockStoreReader(store, key, block_size=4 * 2**20) as reader, h5py.File(reader, "r") as f:
        for name, array in sorted(manifest_store._group.arrays.items()):
            if name not in f:
                out["error"].append(f"{name} (in the sidecar, not in the file)")
                continue
            why = comparable(array, max_chunks, max_bytes)
            if why:
                out["not compared"].append(f"{name} ({why})")
                continue
            try:
                through_sidecar, direct = group[name][...], f[name][...]
                same = through_sidecar.shape == direct.shape and (
                    np.array_equal(through_sidecar, direct, equal_nan=True) if direct.dtype.kind in "fc" else np.array_equal(through_sidecar, direct)
                )
                out["equal" if same else "DIFFERENT"].append(name)
            except Exception as error:  # noqa: BLE001 -- one variable's failure is a result
                out["error"].append(f"{name} ({type(error).__name__}: {' '.join(str(error).split())[:80]})")
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("urls", nargs="+", help="s3:// (in us-west-2) or https:// URLs of HDF5 or netCDF-4 granules; the sidecar is <url>.dmrpp")
    p.add_argument("--provider", default="POCLOUD", help="CMR provider whose S3 credentials to fetch")
    p.add_argument("--token", default=os.environ.get("EARTHDATA_TOKEN"), help="Earthdata bearer token for https:// URLs (default: EARTHDATA_TOKEN)")
    p.add_argument("--max-chunks", type=int, default=200, help="skip variables with more chunks than this")
    p.add_argument("--max-mb", type=int, default=64, help="skip variables larger than this, uncompressed")
    args = p.parse_args(argv)
    cache: dict[str, Any] = {}
    bad = 0
    for url in args.urls:
        print(f"== {url.rsplit('/', 1)[-1]}")
        try:
            result = check(url, store_for(url, args.provider, args.token, cache), args.max_chunks, args.max_mb * 2**20)
        except Exception as error:  # noqa: BLE001 -- report and go on to the next granule
            print(f"   FAILED: {type(error).__name__}: {' '.join(str(error).split())[:200]}")
            bad += 1
            continue
        for label, names in result.items():
            if names or label in ("equal", "DIFFERENT"):
                shown = "" if label == "equal" else ": " + ", ".join(names[:8]) + (f", and {len(names) - 8} more" if len(names) > 8 else "")
                print(f"   {label}: {len(names)}{shown}")
        bad += len(result["DIFFERENT"]) + len(result["error"])
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
