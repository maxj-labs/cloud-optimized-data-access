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
#     "scipy==1.18.1",
# ]
# ///
"""Is a DMR++ sidecar still right for the file beside it?

A sidecar lists a byte offset and length for every chunk. This script reads
the same list a second way, from the file's own HDF5 metadata, and compares
the two chunk by chunk. Only metadata is read; no science data moves.

    uv run verify_dmrpp.py stale_granules.csv --out verdicts.csv            # in us-west-2, direct S3
    uv run verify_dmrpp.py stale_granules.csv --transport https             # anywhere, slower
    uv run verify_dmrpp.py https://archive.podaac.earthdata.nasa.gov/.../file.nc --transport https

The CSV is either inventory's list of stale files: the sampler's
``stale_granules.csv`` or the listing script's ``stale.csv``. ``--collection``
keeps only the named collections. A bare URL is a data file's; its sidecar
is ``<url>.dmrpp``.

    uv run verify_dmrpp.py stale.csv --collection L3S_LEO_PM-STAR-v2.81 --min-gap-days 1 --per-collection 10

Needs an Earthdata login: ``EARTHDATA_TOKEN``, or ``EARTHDATA_USERNAME`` and
``EARTHDATA_PASSWORD``, or a netrc entry.

Verdicts, one row per granule:

- ``match``: every chunk both sides list has the same offset and length, and
  every shared variable has the same shape, data type, chunk shape and filters.
- ``MISMATCH``: at least one chunk differs. The sidecar points at the wrong bytes.
- ``METADATA_MISMATCH``: the bytes line up but the sidecar describes them
  differently (shape, data type, chunk shape or the set of filters).
- ``SIDECAR_PAST_END``: the sidecar references bytes beyond the end of the file.
- ``nothing_to_compare``: no chunked variable appears on both sides.
- ``not_hdf5``: the file is HDF4 or another format with no file-side parser
  here. HDF5, netCDF-4 and netCDF classic files are all judged.
- ``error <stage>: ...``: a failure, with the step it happened in (reading the
  sidecar, or reading the file's own index). ``--traceback`` prints it in full.

Variables only one side lists are counted, not failed: a sidecar may inline
small variables and omit chunks that are all fill value.

``--values`` then asks the question a user cares about: does the service
return the right numbers? For each chosen granule it requests a block from
the middle of up to ``--value-variables`` variables from Hyrax (OPeNDAP) and
compares it with a direct read of the same block from the file. A block is
at most ``--value-edge`` elements per dimension (256 by default), so the
check costs the same for a small variable and a global grid. Variables the
sidecar describes differently from the file are checked first. The ``hyrax_values`` column is one of:

- ``identical``: every variable checked equals the direct read.
- ``DIFFERENT``: Hyrax answered with other numbers and no error. This is
  silent wrong data.
- ``hyrax error: ...``: the request failed, with Hyrax's message. Loud.
- ``skipped: ...``: nothing could be compared, with the reason.

``--values mismatch`` (the default when the flag is given bare) checks only
granules that did not come back ``match``; ``--values all`` checks every
granule, which is how a random sample of healthy files becomes a control.

The exit code is 2 if any granule is ``DIFFERENT``, else 1 if any sidecar
is wrong, else 0.
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
FIELDS = ["verdict", "provider", "short_name", "granule_ur", "file_format", "data_newer_by_days", "variables_compared", "chunks_compared",
          "chunks_mismatched", "variables_mismatched", "only_in_sidecar", "only_in_file", "file_size", "sidecar_max_byte",
          "first_mismatch", "metadata_differences", "filter_order_differs", "int8_declared_int16", "file_int8_variables",
          "sidecar_version", "sidecar_cf_markers", "sidecar_hdf5_global", "sidecar_int8_variables", "sidecar_flattened", "hyrax_values", "hyrax_detail", "data_url"]
HYRAX = "https://opendap.earthdata.nasa.gov"
HYRAX_TIMEOUT_S = 120  # per request; a slow or failing Hyrax answer is recorded, not waited on
FILTER_NAMES = {"zlib": "deflate", "gzip": "deflate", "deflate": "deflate", "shuffle": "shuffle", "fletcher32": "fletcher32", "zstd": "zstd", "blosc": "blosc", "lz4": "lz4"}


def manifests(group: Any, prefix: str = "", recurse: bool = True) -> dict[str, dict[str, dict[str, Any]]]:
    """{variable path: {chunk key: {path, offset, length}}} for every array under a ManifestGroup."""
    out = {f"{prefix}/{name}": array.manifest.dict() for name, array in group.arrays.items()}
    for name, child in group.groups.items() if recurse else ():
        out.update(manifests(child, f"{prefix}/{name}"))
    return out


def group_paths(group: Any, prefix: str = "") -> list[str]:
    """Paths of every group that holds arrays ("" is the root)."""
    out = [prefix] if group.arrays else []
    for name, child in group.groups.items():
        out += group_paths(child, f"{prefix}/{name}")
    return out


def filters(metadata: Any) -> list[str]:
    """The array's filters in order, by common name; the bytes (endianness) codec is left out."""
    names = []
    for codec in metadata.codecs:
        config = getattr(codec, "codec_config", None) or {}
        name = str(config.get("id") or getattr(codec, "codec_name", "") or type(codec).__name__).rsplit(".", 1)[-1].lower()
        if name in ("bytes", "bytescodec"):
            continue
        names.append(FILTER_NAMES.get(name, name))
    return names


def describe(group: Any, prefix: str = "", recurse: bool = True) -> dict[str, dict[str, Any]]:
    """{variable path: shape, data type, chunk shape and filters} for every array under a ManifestGroup."""
    out = {}
    for name, array in group.arrays.items():
        m = array.metadata
        out[f"{prefix}/{name}"] = {"shape": tuple(m.shape), "dtype": str(m.data_type), "chunks": tuple(m.chunk_grid.chunk_shape), "filters": filters(m)}
    for name, child in group.groups.items() if recurse else ():
        out.update(describe(child, f"{prefix}/{name}"))
    return out


def compare_metadata(sidecar: dict[str, dict[str, Any]], file: dict[str, dict[str, Any]]) -> tuple[list[str], int]:
    """(differences that change what the bytes mean, variables whose filters agree as a set but not in order).

    Fill values and compression levels are left out: the two parsers report
    them differently for the same file, and neither changes which bytes are read.
    Filter order is counted but not failed for the same reason."""
    differences, reordered = [], 0
    for name in sorted(set(sidecar) & set(file)):
        a, b = sidecar[name], file[name]
        for key in ("shape", "dtype", "chunks"):
            if a[key] != b[key]:
                differences.append(f"{name} {key}: sidecar {short(a[key])}, file {short(b[key])}")
        if sorted(a["filters"]) != sorted(b["filters"]):
            differences.append(f"{name} filters: sidecar {a['filters']}, file {b['filters']}")
        elif a["filters"] != b["filters"]:
            reordered += 1
    return differences, reordered


def short(value: Any) -> str:
    """Float64(endianness='little') → Float64 little; anything else as it is."""
    text = str(value)
    return text.replace("(endianness='", " ").replace("')", "").replace("()", "") if "endianness" in text or text.endswith("()") else text


def compare(
    sidecar: dict[str, dict[str, dict[str, Any]]], file: dict[str, dict[str, dict[str, Any]]], file_size: int | None,
    sidecar_meta: dict[str, dict[str, Any]] | None = None, file_meta: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
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
    differences, reordered = compare_metadata(sidecar_meta or {}, file_meta or {})
    if file_size is not None and max_byte > file_size:
        verdict = "SIDECAR_PAST_END"
    elif mismatched:
        verdict = "MISMATCH"
    elif differences:
        verdict = "METADATA_MISMATCH"
    elif chunks == 0:
        verdict = "nothing_to_compare"
    else:
        verdict = "match"
    return {
        "verdict": verdict, "variables_compared": len(shared), "chunks_compared": chunks, "chunks_mismatched": mismatched,
        "variables_mismatched": len(bad_variables), "only_in_sidecar": len(set(sidecar) - set(file)), "only_in_file": len(set(file) - set(sidecar)),
        "file_size": file_size, "sidecar_max_byte": max_byte, "first_mismatch": first,
        "metadata_differences": "; ".join(differences[:5]) + (f"; and {len(differences) - 5} more" if len(differences) > 5 else ""),
        "filter_order_differs": reordered,
        # for the value check: variables whose bytes are wrong first, then those the sidecar describes differently
        "mismatched_variables": bad_variables + [n for n in dict.fromkeys(d.split(" ", 1)[0] for d in differences) if n not in bad_variables],
    }


def pick(rows: list[dict[str, str]], per_collection: int, min_gap_days: float | None, limit: int | None, seed: int = 0) -> list[dict[str, str]]:
    """Up to ``per_collection`` granules from each collection, largest gaps first across collections."""
    by: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if min_gap_days is None or float(row.get("data_newer_by_s") or 0) >= min_gap_days * 86400:
            by[row["concept_id"]].append(row)
    rng = random.Random(seed)
    chosen = []
    for group in sorted(by.values(), key=lambda g: -max(float(r.get("data_newer_by_s") or 0) for r in g)):
        chosen += rng.sample(group, min(per_collection, len(group)))
    return chosen[:limit] if limit else chosen


def normalise(row: dict[str, str]) -> dict[str, str]:
    """Accept the listing script's stale.csv too: its rows carry ``bucket`` and ``path`` instead of ``data_url``."""
    if not row.get("data_url") and row.get("bucket") and row.get("path"):
        row = {**row, "data_url": f"s3://{row['bucket']}/{row['path']}"}
    if not row.get("provider"):
        row = {**row, "provider": (row.get("concept_id") or "").rpartition("-")[2]}
    return row


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

    def token(self) -> str:
        """An Earthdata bearer token, for Hyrax."""
        token = getattr(self.auth, "token", None) or {}
        if not token.get("access_token"):
            raise RuntimeError("no Earthdata token available for Hyrax; set EARTHDATA_TOKEN")
        return token["access_token"]

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


def verify(
    row: dict[str, str], access: Access, traceback_wanted: bool = False, values: str = "none", value_variables: int = 3,
    value_edge: int = 256,
) -> dict[str, Any]:
    from xml.etree import ElementTree

    import obstore
    from obspec_utils.registry import ObjectStoreRegistry
    from virtualizarr.parsers import HDFParser
    from virtualizarr.parsers.dmrpp import DMRParser

    out: dict[str, Any] = {
        "provider": row.get("provider"), "short_name": row.get("short_name"), "granule_ur": row.get("granule_ur"), "data_url": row["data_url"],
        "data_newer_by_days": round(float(row["data_newer_by_s"]) / 86400, 2) if row.get("data_newer_by_s") else None,
    }
    stage = "locating the file"
    try:
        url, (root, store) = access.locate(row)
        registry = ObjectStoreRegistry({root: store})
        key = url[len(root):].lstrip("/")
        size = int(obstore.head(store, key)["size"])
        magic = bytes(obstore.get_range(store, key, start=0, end=8))
        file_meta: dict[str, dict[str, Any]] = {}
        if magic.startswith(b"CDF"):
            from virtualizarr.parsers import NetCDF3Parser

            file_parser, out["file_format"] = NetCDF3Parser(), "netCDF classic"
        elif magic.startswith(b"\x89HDF"):
            file_parser, out["file_format"] = HDFParser(), "HDF5"
        else:
            out["verdict"] = "not_hdf5"  # HDF4 or something else: no file-side parser here can index it
            return out
        stage = "reading the file's own index"
        file_group = file_parser(url, registry)._group
        file_meta = describe(file_group)
        stage = "reading the sidecar"
        sidecar_text = bytes(obstore.get(store, key + ".dmrpp").bytes())
        out.update(sidecar_signs(sidecar_text))
        out["file_int8_variables"] = sum(m["dtype"].startswith("Int8") for m in file_meta.values())
        sidecar_root = ElementTree.fromstring(sidecar_text)
        # the sidecar parser returns one group per call, so ask for each group the file has
        sidecar_index: dict[str, dict[str, dict[str, Any]]] = {}
        sidecar_meta: dict[str, dict[str, Any]] = {}
        has_groups = b"<Group " in sidecar_text
        for path in (group_paths(file_group) or [""]) if has_groups else [""]:
            part = DMRParser(root=sidecar_root, data_filepath=url).parse_dataset(object_store=store, group=path or "/")._group
            sidecar_index.update(manifests(part, path, recurse=False))
            sidecar_meta.update(describe(part, path, recurse=False))
        stage = "comparing"
        file_index = manifests(file_group)
        renamed = unflatten(set(sidecar_index), set(file_index))
        if renamed:
            # an older sidecar with no groups: /data_01/ku/time is listed as /data_01_ku_time
            sidecar_index = {renamed.get(n, n): v for n, v in sidecar_index.items()}
            sidecar_meta = {renamed.get(n, n): v for n, v in sidecar_meta.items()}
        out["sidecar_flattened"] = bool(renamed)
        out.update(compare(sidecar_index, file_index, size, sidecar_meta, file_meta))
        out["int8_declared_int16"] = sum(
            file_meta[n]["dtype"].startswith("Int8") and sidecar_meta[n]["dtype"].startswith("Int16") for n in set(file_meta) & set(sidecar_meta)
        )
    except Exception as error:  # noqa: BLE001 -- one unreadable granule is a row, not the end of the run
        out["verdict"] = f"error {stage}: {type(error).__name__}: {' '.join(str(error).split())[:200]}"
        if traceback_wanted:
            import traceback

            traceback.print_exc()
    wanted = values == "all" or (values == "mismatch" and out["verdict"] != "match")
    if wanted and "store" in locals() and out.get("file_format"):
        try:
            out["hyrax_values"], out["hyrax_detail"] = check_values(
                row, access, store, key, size, out["file_format"], file_meta if "file_meta" in locals() else {},
                out.get("mismatched_variables") or [], value_variables, value_edge,
            )
        except Exception as error:  # noqa: BLE001 -- the value check failing is a result, not a crash
            out["hyrax_values"] = f"skipped: {type(error).__name__}: {' '.join(str(error).split())[:200]}"
            if traceback_wanted:
                import traceback

                traceback.print_exc()
    return out


def unflatten(sidecar_names: set[str], file_names: set[str]) -> dict[str, str]:
    """{sidecar name: file name} where a sidecar without groups names a grouped
    variable by joining its path with underscores. Empty unless that explains
    names the two sides do not already share."""
    if sidecar_names & file_names:
        return {}
    flat = {"/" + name.strip("/").replace("/", "_"): name for name in file_names if name.count("/") > 1}
    return {name: flat[name] for name in sidecar_names if name in flat}


def sidecar_signs(text: bytes) -> dict[str, Any]:
    """How a sidecar's variable descriptions appear to have been produced, from its text alone.

    ``sidecar_cf_markers`` counts the ``fullnamepath`` attributes: older sidecars
    carry one per variable, newer ones none. The hypothesis under test is that
    those older sidecars are the ones that declare signed 8-bit data as Int16."""
    import re

    version = re.search(rb'dmrpp:version="([^"]+)"', text)
    return {
        "sidecar_version": version.group(1).decode() if version else "",
        "sidecar_cf_markers": text.count(b'name="fullnamepath"'),
        "sidecar_hdf5_global": b'name="HDF5_GLOBAL"' in text,
        "sidecar_int8_variables": len(re.findall(rb"<Int8 name=", text)),
    }


def granule_identity(row: dict[str, str]) -> tuple[str, str]:
    """(collection concept id, GranuleUR) for Hyrax's URL, from the row or from CMR by file name."""
    if row.get("concept_id") and row.get("granule_ur"):
        return row["concept_id"], row["granule_ur"]
    name = row["data_url"].rsplit("/", 1)[-1]
    scope = {"collection_concept_id": row["concept_id"]} if row.get("concept_id") else {"provider": row["provider"]} if row.get("provider") else {}
    items: list[dict[str, Any]] = []
    for candidate in (name, name.rsplit(".", 1)[0]):  # CMR knows a granule by its file name or by the name without the extension
        query = urllib.parse.urlencode({"readable_granule_name": candidate, "page_size": 5, **scope})
        with urllib.request.urlopen(f"{CMR}?{query}", timeout=60) as response:
            items = json.load(response).get("items", [])
        if items:
            break
    if not items:
        raise LookupError(f"CMR has no granule named {name}")
    return items[0]["meta"]["collection-concept-id"], items[0]["umm"]["GranuleUR"]


def slab(shape: tuple[int, ...], edge: int) -> tuple[slice, ...]:
    """A block of at most ``edge`` elements per dimension from the middle of an array.

    The value check reads this block, not the whole variable: it is one small
    request to Hyrax and a few chunks from the file, however large the variable is."""
    return tuple(slice((n - min(n, edge)) // 2, (n - min(n, edge)) // 2 + min(n, edge)) for n in shape)


def constraint(name: str, block: tuple[slice, ...]) -> str:
    """The DAP4 constraint expression for ``block`` of ``name``: /v[10:1:19][0:1:4]."""
    return name + "".join(f"[{s.start}:1:{s.stop - 1}]" for s in block)


def direct_values(store: Any, key: str, size: int, file_format: str, blocks: dict[str, tuple[slice, ...]]) -> dict[str, Any]:
    """The stored (unscaled) values of each variable's block, read from the file itself."""
    import io

    import numpy as np

    if file_format == "HDF5":
        import h5py
        from obspec_utils.readers import BlockStoreReader

        with BlockStoreReader(store, key, block_size=4 * 2**20) as reader, h5py.File(reader, "r") as f:
            return {name: np.asarray(f[name][block]) for name, block in blocks.items()}
    import obstore
    from scipy.io import netcdf_file

    if size > 256 * 2**20:
        raise ValueError("netCDF classic file over 256 MiB: not read whole")
    with netcdf_file(io.BytesIO(bytes(obstore.get(store, key).bytes())), "r", mmap=False, maskandscale=False) as f:
        return {name: np.array(f.variables[name.lstrip("/")][block]) for name, block in blocks.items()}


def hyrax_values(token: str, concept_id: str, granule_ur: str, name: str, block: tuple[slice, ...] | None = None) -> Any:
    """One variable's stored values (or one block of them) as Hyrax serves them, in a
    netCDF-4 response. Raises RuntimeError with Hyrax's message."""
    import re
    import tempfile
    import urllib.error

    import h5py
    import numpy as np

    url = f"{HYRAX}/collections/{concept_id}/granules/{urllib.parse.quote(granule_ur)}.dap.nc4?dap4.ce={urllib.parse.quote(constraint(name, block) if block else name)}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": "verify-dmrpp/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=HYRAX_TIMEOUT_S) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        text = re.sub(r"<[^>]+>|\s+", " ", error.read().decode(errors="replace"))
        message = re.search(r"associated with your request was:\s*(.*?)\s*If you think", text)
        if error.code == 504:
            raise RuntimeError("504 gateway timeout") from None
        raise RuntimeError(f"{error.code} {message.group(1) if message else ' '.join(text.split())[-200:]}") from None
    except (TimeoutError, urllib.error.URLError) as error:
        raise RuntimeError(f"no answer within {HYRAX_TIMEOUT_S} s ({type(error).__name__})") from None
    if not body.startswith(b"\x89HDF"):
        raise RuntimeError(f"not a netCDF-4 response: {body[:80]!r}")
    with tempfile.NamedTemporaryFile(suffix=".nc4") as scratch:
        scratch.write(body)
        scratch.flush()
        with h5py.File(scratch.name, "r") as f:
            return np.asarray(f[name][...])


def same_values(a: Any, b: Any) -> bool:
    import numpy as np

    if a.shape != b.shape:
        return False
    if a.dtype.kind in "fc" or b.dtype.kind in "fc":
        return bool(np.array_equal(a, b, equal_nan=True))
    return bool(np.array_equal(a, b))


def choose_variables(file_meta: dict[str, dict[str, Any]], mismatched: list[str], limit: int, max_bytes: int | None = None) -> list[str]:
    """Numeric arrays: the mismatched ones first, then the largest. ``max_bytes``
    leaves out larger variables; None keeps them all (the check reads a block, not the whole)."""
    import math
    import re

    def nbytes(meta: dict[str, Any]) -> int:
        bits = re.search(r"(\d+)", meta["dtype"])
        return math.prod(meta["shape"]) * (int(bits.group(1)) // 8 if bits else 8)

    numeric = {n: nbytes(m) for n, m in file_meta.items() if re.match(r"(U?Int|Float)\d+", m["dtype"]) and m["shape"] and 0 < nbytes(m) and (max_bytes is None or nbytes(m) <= max_bytes)}
    ordered = [n for n in mismatched if n in numeric] + sorted((n for n in numeric if n not in mismatched), key=lambda n: -numeric[n])
    return ordered[:limit]


def check_values(
    row: dict[str, str], access: Access, store: Any, key: str, size: int, file_format: str, file_meta: dict[str, dict[str, Any]],
    mismatched: list[str], limit: int, edge: int,
) -> tuple[str, str]:
    """(hyrax_values, detail): compare what Hyrax serves with a direct read, one block per variable."""
    names = choose_variables(file_meta, mismatched, limit)
    if not names:
        return "skipped: no numeric variable to compare", ""
    concept_id, granule_ur = granule_identity(row)
    blocks = {name: slab(file_meta[name]["shape"], edge) for name in names}
    truth = direct_values(store, key, size, file_format, blocks)
    results = {}
    for name in names:
        try:
            served = hyrax_values(access.token(), concept_id, granule_ur, name, blocks[name])
            results[name] = "identical" if same_values(served.reshape(truth[name].shape) if served.size == truth[name].size else served, truth[name]) else "DIFFERENT"
        except RuntimeError as error:
            results[name] = f"hyrax error: {error}"
    detail = "; ".join(f"{name}: {result}" for name, result in results.items())
    if any(r == "DIFFERENT" for r in results.values()):
        return "DIFFERENT", detail
    errors = [r for r in results.values() if r.startswith("hyrax error")]
    if errors:
        return errors[0][:160], detail
    return "identical", detail


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("inputs", nargs="+", help="stale_granules.csv, or data file URLs")
    p.add_argument("--transport", choices=("s3", "https"), default="s3", help="s3: direct, from us-west-2 (default); https: the DAAC endpoint, from anywhere")
    p.add_argument("--per-collection", type=int, default=2, help="granules to check per collection (CSV input)")
    p.add_argument("--min-gap-days", type=float, default=None, help="only granules whose data file is at least this much newer than its sidecar (default: no filter)")
    p.add_argument("--provider", nargs="*", help="only these providers from the CSV, e.g. POCLOUD")
    p.add_argument("--collection", nargs="*", help="only these collections (short names or concept ids) from the CSV")
    p.add_argument("--limit", type=int, help="stop after this many granules")
    p.add_argument("--values", nargs="?", const="mismatch", default="none", choices=("none", "mismatch", "all"),
                   help="also compare what Hyrax serves with a direct read: for granules that did not match (default when given), or for all")
    p.add_argument("--value-variables", type=int, default=3, help="variables to compare per granule")
    p.add_argument("--value-edge", type=int, default=256, help="compare a block of at most this many elements per dimension, from the middle of each variable")
    p.add_argument("--start", type=int, default=1, help="begin at this granule number (as printed), to continue an interrupted run")
    p.add_argument("--traceback", action="store_true", help="print the full traceback when a granule errors")
    p.add_argument("--out", help="write verdicts to this CSV as well as the screen")
    args = p.parse_args(argv)

    rows: list[dict[str, str]] = []
    for item in args.inputs:
        if item.endswith(".csv"):
            with open(item, newline="") as f:
                found = [normalise(r) for r in csv.DictReader(f)]
            if args.provider:
                found = [r for r in found if r.get("provider") in args.provider]
            if args.collection:
                found = [r for r in found if r.get("short_name") in args.collection or r.get("concept_id") in args.collection]
            rows += pick(found, args.per_collection, args.min_gap_days, None)
        else:
            rows.append({"data_url": item.strip(), "provider": os.environ.get("CMR_PROVIDER", "")})
    rows = rows[: args.limit] if args.limit else rows
    print(f"{len(rows)} granules to verify over {args.transport}", file=sys.stderr)
    access = Access(args.transport)
    sink = open(args.out, "w", newline="") if args.out else None  # noqa: SIM115 -- closed below; rows are flushed as they arrive
    writer = csv.DictWriter(sink, fieldnames=FIELDS, extrasaction="ignore") if sink else None
    if writer:
        writer.writeheader()
    tally: dict[str, int] = defaultdict(int)
    hyrax: dict[str, int] = defaultdict(int)
    cross: dict[tuple[str, str], int] = defaultdict(int)
    for index, row in enumerate(rows, 1):
        if index < args.start:
            continue
        result = verify(row, access, args.traceback, args.values, args.value_variables, args.value_edge)
        tally["error" if result["verdict"].startswith("error") else result["verdict"]] += 1
        detail = f"{result.get('chunks_mismatched')} of {result.get('chunks_compared')} chunks differ" if "chunks_compared" in result else ""
        print(f"[{index}/{len(rows)}] {result['verdict']:<18} {result.get('short_name') or ''} {row['data_url'].rsplit('/', 1)[-1]} {detail}", flush=True)
        if "sidecar_cf_markers" in result and result.get("file_int8_variables") is not None:
            if not result.get("variables_compared"):
                wrong = "not compared"
            elif result.get("int8_declared_int16"):
                wrong = "8-bit declared as Int16"
            else:
                wrong = "8-bit declared correctly" if result["file_int8_variables"] else "no signed 8-bit variables"
            cross[("older sidecar (has fullnamepath)" if result["sidecar_cf_markers"] else "newer sidecar (no fullnamepath)", wrong)] += 1
        for note in (result.get("first_mismatch"), result.get("metadata_differences")):
            if note:
                print(f"      {note}", flush=True)
        if result.get("hyrax_values"):
            hyrax[result["hyrax_values"].split(":")[0]] += 1
            print(f"      Hyrax values: {result['hyrax_values']}" + (f" ({result['hyrax_detail']})" if result.get("hyrax_detail") else ""), flush=True)
        if writer and sink:
            writer.writerow(result)
            sink.flush()
    if sink:
        sink.close()
    print("\n" + ", ".join(f"{k}: {v}" for k, v in sorted(tally.items())))
    if cross:
        print("\nSidecar kind against how it declares the file's signed 8-bit variables:")
        for (kind, outcome), n in sorted(cross.items()):
            print(f"  {kind:36s} {outcome:28s} {n}")
    if hyrax:
        print("Hyrax values: " + ", ".join(f"{k}: {v}" for k, v in sorted(hyrax.items())))
    if hyrax.get("DIFFERENT"):
        return 2
    return 1 if tally.get("MISMATCH") or tally.get("SIDECAR_PAST_END") or tally.get("METADATA_MISMATCH") else 0


if __name__ == "__main__":
    sys.exit(main())
