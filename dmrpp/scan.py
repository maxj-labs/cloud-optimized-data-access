"""Every sidecar of a collection: does it carry the mark of the Int16 fault?

``python -m dmrpp.border`` samples a collection and reports a border when
there is one. This reads the start of every sidecar instead, so the answer
is a count: how many are "flagged" (no ``dmrpp:version`` attribute and
``fullnamepath`` attributes on the variables, as in OPENDAP/hyrax#17) and how
many are "clear". It lists the collection's S3 prefix and reads the first
8 KB of each ``.dmrpp`` object.

Run in us-west-2 with an Earthdata login (EARTHDATA_TOKEN, username and
password, or netrc). Try one small collection first:

    uv run --extra dmrpp python -m dmrpp.scan --short-name AMSR2-REMSS-L3U_RT-v8a --out scan
    uv run --extra dmrpp python -m dmrpp.scan --int16-findings results/dmrpp/verify/verdicts.csv --out scan

It writes ``<out>/summary.csv``, one row per collection, and
``<out>/sidecars/<short_name>.csv.gz``, one row per sidecar. Collections are
scanned smallest first when the border search's ``borders.csv`` is there to say
how big they are. A rerun skips the collections in ``summary.csv`` that
finished and takes up an unfinished one after the last sidecar it wrote. The
DAAC's temporary credentials last an hour, so a long listing is restarted
from where it was with new ones.

A flag is the mark the faulty sidecars share, not a comparison with the file:
that is the verifier's.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import gzip
import shutil
import sys
import time
import urllib.parse
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dmrpp.border import COLLECTIONS, SIDECAR_HEAD_BYTES, chosen, flagged
from dmrpp.border import RESULTS as BORDERS
from dmrpp.common import CREDENTIALS_RENEW_AFTER_S
from dmrpp.common import SIDECAR_SUFFIX as SIDECAR
from dmrpp.survey import is_sidecar

ROW_FIELDS = ["path", "size", "last_modified", "kind", "dmrpp_version", "fullnamepath"]
SUMMARY_FIELDS = [
    "concept_id", "short_name", "prefix", "error", "sidecars", "flagged", "clear", "clear_unversioned",
    "not_a_sidecar", "unreadable", "flagged_last_modified_from", "flagged_last_modified_to", "seconds",
]  # fmt: skip
READ_TRIES = 3


def classify(head: bytes) -> dict[str, Any]:
    if not is_sidecar(head):
        return {"kind": "not_a_sidecar"}
    flag, version, marked = flagged(head)
    return {"kind": "flagged" if flag else "clear", "dmrpp_version": version or "", "fullnamepath": marked}


async def read_head(store: Any, path: str, gate: asyncio.Semaphore) -> dict[str, Any]:
    import obstore

    async with gate:
        for attempt in range(READ_TRIES):
            try:
                return classify(bytes(await obstore.get_range_async(store, path, start=0, end=SIDECAR_HEAD_BYTES)))
            except Exception as error:  # noqa: BLE001 -- a sidecar that cannot be read is a row, after retries
                failure = f"error: {type(error).__name__}"
                await asyncio.sleep(2**attempt if attempt < READ_TRIES - 1 else 0)
    return {"kind": failure}


def last_path(rows: Path) -> str | None:
    """The last sidecar an unfinished scan wrote: where to take the listing up again."""
    if not rows.exists():
        return None
    with rows.open(newline="") as f:
        last = None
        for row in csv.DictReader(f):
            last = row["path"]
    return last


async def scan_prefix(
    get_store: Callable[[], Any],
    key_prefix: str,
    rows: Path,
    concurrency: int = 128,
    renew_after_s: float = CREDENTIALS_RENEW_AFTER_S,
    progress: Callable[[int], None] | None = None,
) -> None:
    """Append one row per sidecar under ``key_prefix`` to ``rows``, in listing
    order. ``get_store`` is called again whenever the store is older than
    ``renew_after_s``, and the listing goes on after the last object seen."""
    import obstore

    offset = last_path(rows)
    fresh = not rows.exists()
    gate = asyncio.Semaphore(concurrency)
    written = 0
    with rows.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ROW_FIELDS)
        if fresh:
            writer.writeheader()
        while True:
            store, born, finished = get_store(), time.monotonic(), True
            async for page in obstore.list(store, prefix=key_prefix or None, offset=offset, chunk_size=1000):
                sidecars = [m for m in page if str(m["path"]).endswith(SIDECAR)]
                heads = await asyncio.gather(*(read_head(store, str(m["path"]), gate) for m in sidecars))
                writer.writerows(
                    {
                        "path": str(m["path"]),
                        "size": m["size"],
                        "last_modified": m["last_modified"].isoformat(),
                        **head,
                    }
                    for m, head in zip(sidecars, heads, strict=True)
                )
                f.flush()
                written += len(sidecars)
                if progress:
                    progress(written)
                if page:
                    offset = str(page[-1]["path"])
                if time.monotonic() - born > renew_after_s:
                    finished = False
                    break
            if finished:
                return


def summarize(rows: Path) -> dict[str, Any]:
    kinds: Counter[str] = Counter()
    clear_unversioned = 0
    flagged_modified: list[str] = []
    with rows.open(newline="") as f:
        for row in csv.DictReader(f):
            kinds["unreadable" if row["kind"].startswith("error") else row["kind"]] += 1
            clear_unversioned += row["kind"] == "clear" and not row["dmrpp_version"]
            if row["kind"] == "flagged":
                flagged_modified.append(row["last_modified"])
    return {
        "sidecars": sum(kinds.values()),
        "flagged": kinds["flagged"],
        "clear": kinds["clear"],
        "clear_unversioned": clear_unversioned,
        "not_a_sidecar": kinds["not_a_sidecar"],
        "unreadable": kinds["unreadable"],
        "flagged_last_modified_from": min(flagged_modified, default=""),
        "flagged_last_modified_to": max(flagged_modified, default=""),
    }


def prefix_of(concept_id: str, up: int) -> str:
    from dmrpp.list_collection import prefix_from_cmr

    for attempt in range(6):  # CMR sometimes does not answer a search it answers when asked again
        try:
            return prefix_from_cmr(concept_id, up)
        except OSError:
            time.sleep(2**attempt)
    raise RuntimeError(f"CMR did not answer for {concept_id}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--collections", default=COLLECTIONS, help="the survey's collections.csv: short_name to concept_id")
    p.add_argument("--short-name", action="append", help="a collection to scan; may be repeated")
    p.add_argument(
        "--int16-findings", help="a verdicts.csv: scan every collection with a signed 8-bit variable declared Int16"
    )
    p.add_argument("--out", required=True, help="directory for summary.csv and sidecars/<short_name>.csv.gz")
    p.add_argument("--sizes", default=f"{BORDERS}/borders.csv", help="a borders.csv, for its granule counts")
    p.add_argument(
        "--up", type=int, default=0, help="climb this many more directories after date-like ones are dropped"
    )
    p.add_argument("--concurrency", type=int, default=128, help="sidecar reads in flight")
    p.add_argument("--region", default="us-west-2")
    args = p.parse_args(argv)
    if not (args.short_name or args.int16_findings):
        p.error("give --short-name or --int16-findings")

    from dmrpp.list_collection import Stores

    out = Path(args.out)
    (out / "sidecars").mkdir(parents=True, exist_ok=True)
    summary = out / "summary.csv"
    kept: list[dict[str, str]] = []
    if summary.exists():
        with summary.open(newline="") as f:
            kept = [r for r in csv.DictReader(f) if not r["error"]]
    with summary.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        writer.writerows(kept)
    done = {r["concept_id"] for r in kept}
    todo = [c for c in chosen(args) if c["concept_id"] not in done]
    sizes: dict[str, int] = {}
    if Path(args.sizes).exists():  # the border search's granule counts, to scan the small collections first
        with Path(args.sizes).open(newline="") as f:
            sizes = {r["concept_id"]: int(r["granules"] or 0) for r in csv.DictReader(f)}
    todo.sort(key=lambda c: sizes.get(c["concept_id"], 0))
    print(f"{len(todo)} collections to scan ({len(done)} already done)", file=sys.stderr)
    stores = Stores(args.region, anonymous=False)
    failed = 0
    for index, collection in enumerate(todo, 1):
        row: dict[str, Any] = {"concept_id": collection["concept_id"], "short_name": collection["short_name"]}
        rows = out / "sidecars" / f"{collection['short_name']}.csv"
        start = time.perf_counter()
        print(f"[{index}/{len(todo)}] {collection['short_name']}", file=sys.stderr, flush=True)
        try:
            row["prefix"] = prefix_of(collection["concept_id"], args.up)
            parsed = urllib.parse.urlparse(row["prefix"])
            bucket, provider = parsed.netloc, collection["provider"]

            shown = [0]

            def report(written: int, shown: list[int] = shown, start: float = start) -> None:
                if written - shown[0] >= 100_000:
                    shown[0] = written
                    print(f"  {written:,} sidecars, {time.perf_counter() - start:,.0f} s", file=sys.stderr, flush=True)

            asyncio.run(
                scan_prefix(
                    lambda bucket=bucket, provider=provider: stores.get(bucket, provider),
                    parsed.path.lstrip("/"),
                    rows,
                    args.concurrency,
                    progress=report,
                )
            )
            row.update(summarize(rows))
            with rows.open("rb") as plain, gzip.open(f"{rows}.gz", "wb") as packed:
                shutil.copyfileobj(plain, packed)
            rows.unlink()
        except Exception as error:  # noqa: BLE001 -- one collection's failure is a row; a rerun takes it up again
            failed += 1
            row["error"] = " ".join(f"{type(error).__name__}: {error}".split())[:300]
        row["seconds"] = round(time.perf_counter() - start, 1)
        with summary.open("a", newline="") as f:
            csv.DictWriter(f, fieldnames=SUMMARY_FIELDS).writerow(row)
        print(
            f"  {row.get('error') or '{:,} sidecars: {:,} flagged, {:,} clear'.format(row['sidecars'], row['flagged'], row['clear'])}",
            file=sys.stderr,
            flush=True,
        )
    print(f"{len(todo) - failed} of {len(todo)} collections scanned; summary in {summary}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
