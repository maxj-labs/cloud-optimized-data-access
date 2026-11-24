"""Where in a collection the sidecars change from one generator to another.

The survey reads a few sidecars at each end of a collection. When the oldest
granules have sidecars with no ``dmrpp:version`` attribute and the newest have
one, somewhere in between is a border. This script finds it and then tests
whether it is a border at all:

1. reads the sidecar of the oldest and of the newest granule (by start date);
2. if they differ, bisects on start date until it holds two neighbouring
   granules whose sidecars differ: first by halving the time window with CMR
   ``temporal`` searches, then, once the window holds few enough granules to
   list, by halving the list;
3. checks for a step change: ``--near`` granules next to the border on each
   side, and ``--spread`` granules spaced evenly in time across each side. A
   step means every one of them agrees with its side. When the two ends agree
   there is nothing to bisect, and the spread check runs across the whole
   collection instead.

A sidecar is "unversioned" when its Dataset element has no ``dmrpp:version``,
else "versioned". One read of a sidecar's first bytes decides it.

It is still a sample. A step verdict says the granules checked agree with a
single border; ``granules_old_side`` is CMR's count of granules that start no
later than the last granule on the old side, which is the number of sidecars
of the old kind only if the step holds everywhere.

Needs ``EARTHDATA_TOKEN``. About 25 CMR searches and 60 sidecar reads per
collection with the defaults.

    EARTHDATA_TOKEN=... uv run --extra dmrpp python -m dmrpp.border --int16-findings results/dmrpp/verify/verdicts.csv
    EARTHDATA_TOKEN=... uv run --extra dmrpp python -m dmrpp.border --short-name RAMSSA_09km-ABOM-L4-AUS-v01
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from dmrpp.common import CMR
from dmrpp.survey import SIDECAR_PREFIX_BYTES, VERSION, data_and_sidecar, is_sidecar

RESULTS = "results/dmrpp/border"
COLLECTIONS = "results/dmrpp/survey/collections.csv"
USER_AGENT = "dmrpp-border/0.1"
LIST_MAX = 500  # a time window with this many granules or fewer is listed and bisected by position
PICK = 10  # granules asked for when one is wanted: the first few can lack a sidecar or start outside the window
MAX_STEPS = 60  # halvings of the time window before giving up; 20 years to one second takes 30
READABLE = ("unversioned", "versioned")
CMR_TIMEOUT_S, CMR_LIST_TIMEOUT_S, CMR_TRIES = 15, 90, 8
FIELDS = [
    "concept_id", "short_name", "verdict", "granules", "old_kind", "new_kind", "new_version",
    "last_old_granule", "last_old_start", "first_new_granule", "first_new_start", "unreadable_between",
    "granules_old_side", "granules_new_side", "old_side_checked", "old_side_disagree", "new_side_checked",
    "new_side_disagree", "disagreeing", "sidecars_read", "detail",
]  # fmt: skip


@dataclass(frozen=True)
class Granule:
    concept_id: str
    ur: str
    start: datetime
    sidecar_url: str | None


@dataclass(frozen=True)
class Sidecar:
    kind: str  # "unversioned" | "versioned" | "none" | "unreadable"
    version: str | None = None
    last_modified: str | None = None
    status: Any = None


class Archive(Protocol):
    """What the search needs from CMR and from the DAAC. ``temporal`` is CMR's:
    granules whose time range overlaps the window, so a search can return a
    granule that starts before it."""

    async def search(
        self, collection: str, *, temporal: tuple[datetime | None, datetime | None] | None, newest_first: bool, n: int
    ) -> tuple[list[Granule], int]: ...

    async def sidecar(self, granule: Granule) -> Sidecar: ...


def start_of(umm: dict[str, Any]) -> datetime | None:
    extent = umm.get("TemporalExtent", {})
    text = extent.get("RangeDateTime", {}).get("BeginningDateTime") or extent.get("SingleDateTime")
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC) if text else None


def granule_of(item: dict[str, Any]) -> Granule | None:
    start = start_of(item["umm"])
    if start is None:
        return None
    return Granule(
        item["meta"]["concept-id"], item["umm"].get("GranuleUR", ""), start, data_and_sidecar(item["umm"])[1]
    )


def cmr_time(t: datetime | None) -> str:
    return "" if t is None else t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def read_sidecar(stat: dict[str, Any]) -> Sidecar:
    """What a ranged read of a sidecar's first bytes says about it."""
    if stat["status"] == 404:
        return Sidecar("none", status=404)
    if stat["status"] not in (200, 206):
        return Sidecar("unreadable", status=stat["status"])
    if not is_sidecar(stat["body"]):
        return Sidecar("none", status=f"{stat['status']}, not a DMR++ document")
    version = VERSION.search(stat["body"])
    return Sidecar(
        "versioned" if version else "unversioned",
        version.group(1).decode() if version else None,
        stat["last_modified"],
        stat["status"],
    )


class Search:
    """One collection's search. Every sidecar read is kept in ``probes``."""

    def __init__(self, archive: Archive, collection: dict[str, str], near: int, spread: int) -> None:
        self.archive, self.collection, self.near, self.spread = archive, collection, near, spread
        self.probes: list[dict[str, Any]] = []
        self.seen: dict[str, Sidecar] = {}

    async def find(
        self, *, temporal: Any = None, newest_first: bool = False, n: int = PICK
    ) -> tuple[list[Granule], int]:
        return await self.archive.search(
            self.collection["concept_id"], temporal=temporal, newest_first=newest_first, n=n
        )

    async def read(self, granule: Granule, role: str) -> Sidecar:
        if granule.concept_id not in self.seen:
            self.seen[granule.concept_id] = (
                await self.archive.sidecar(granule) if granule.sidecar_url else Sidecar("none", status="no data URL")
            )
            sidecar = self.seen[granule.concept_id]
            self.probes.append(
                {
                    "concept_id": self.collection["concept_id"],
                    "short_name": self.collection["short_name"],
                    "granule_concept_id": granule.concept_id,
                    "granule_ur": granule.ur,
                    "start": cmr_time(granule.start),
                    "role": role,
                    "kind": sidecar.kind,
                    "dmrpp_version": sidecar.version,
                    "dmrpp_last_modified": sidecar.last_modified,
                    "dmrpp_status": sidecar.status,
                    "dmrpp_url": granule.sidecar_url,
                }
            )
        return self.seen[granule.concept_id]

    async def first_readable(self, granules: list[Granule], role: str) -> tuple[Granule, Sidecar] | None:
        for granule in granules:
            sidecar = await self.read(granule, role)
            if sidecar.kind in READABLE:
                return granule, sidecar
        return None

    async def bisect(self, lo: Granule, hi: Granule, old_kind: str) -> tuple[Granule, Granule, int] | str:
        """Two granules next to each other in start order, the first of the old
        kind and the second not, and how many granules with no readable
        sidecar lie between them. A string when the search could not finish."""
        after, before = lo.start, hi.start
        for _ in range(MAX_STEPS):
            _, hits = await self.find(temporal=(after, before), n=0)
            if hits <= LIST_MAX:
                break
            middle = after + (before - after) / 2
            found, _ = await self.find(temporal=(middle, before))
            between = [g for g in found if lo.start < g.start < hi.start]
            probed = await self.first_readable(between, "bisect")
            if probed is None:
                before = middle  # nothing to read from the middle on: what is left lies before it
            elif probed[1].kind == old_kind:
                lo, after = probed[0], probed[0].start
            else:
                hi, before = probed[0], probed[0].start
        else:
            return f"the window from {cmr_time(after)} to {cmr_time(before)} still holds more than {LIST_MAX} granules"
        listed, _ = await self.find(temporal=(after, before), n=LIST_MAX)
        inside = sorted(
            (
                g
                for g in listed
                if lo.start <= g.start <= hi.start and g.concept_id not in (lo.concept_id, hi.concept_id)
            ),
            key=lambda g: (g.start, g.concept_id),
        )
        row = [lo, *inside, hi]
        i, j = 0, len(row) - 1
        while j - i > 1:
            m = (i + j) // 2
            # the middle granule, else the nearest one to it with a sidecar that can be read
            order = sorted(range(i + 1, j), key=lambda k: (abs(k - m), k))
            for k in order:
                sidecar = await self.read(row[k], "bisect")
                if sidecar.kind in READABLE:
                    if sidecar.kind == old_kind:
                        i = k
                    else:
                        j = k
                    break
            else:
                break
        return row[i], row[j], j - i - 1

    async def spaced(self, first: Granule, last: Granule, count: int) -> list[Granule]:
        """Up to ``count`` granules that start strictly between two others,
        spaced evenly in time."""
        if count <= 0 or last.start <= first.start:
            return []
        span = last.start - first.start

        async def at(i: int) -> Granule | None:
            found, _ = await self.find(temporal=(first.start + span * (i + 0.5) / count, last.start))
            return next((g for g in found if first.start < g.start < last.start), None)

        chosen = {g.concept_id: g for g in await asyncio.gather(*(at(i) for i in range(count))) if g is not None}
        return list(chosen.values())

    async def check(self, granules: list[Granule], role: str, expected: str) -> tuple[int, list[str]]:
        """(granules with a readable sidecar, those of them that are not of the expected kind)."""
        sidecars = await asyncio.gather(*(self.read(g, role) for g in granules))
        readable = [(g, s) for g, s in zip(granules, sidecars, strict=True) if s.kind in READABLE]
        return len(readable), [f"{g.ur} ({cmr_time(g.start)}) is {s.kind}" for g, s in readable if s.kind != expected]

    async def run(self) -> dict[str, Any]:
        row: dict[str, Any] = {"concept_id": self.collection["concept_id"], "short_name": self.collection["short_name"]}
        oldest, total = await self.find()
        newest, _ = await self.find(newest_first=True)
        row["granules"] = total
        first, last = oldest[0] if oldest else None, newest[0] if newest else None
        lo, hi = await self.first_readable(oldest, "end"), await self.first_readable(newest, "end")
        if first is None or last is None or lo is None or hi is None:
            return {**row, "verdict": "no_readable_sidecar", "sidecars_read": len(self.probes)}
        row.update(old_kind=lo[1].kind, new_kind=hi[1].kind)
        if lo[1].kind == hi[1].kind:
            checked, disagree = await self.check(await self.spaced(first, last, 2 * self.spread), "spread", lo[1].kind)
            return {
                **row,
                "verdict": f"all_{lo[1].kind}" if not disagree else "mixed",
                "new_version": hi[1].version,
                "old_side_checked": checked,
                "old_side_disagree": len(disagree),
                "disagreeing": "; ".join(disagree),
                "sidecars_read": len(self.probes),
            }
        border = await self.bisect(lo[0], hi[0], lo[1].kind)
        if isinstance(border, str):
            return {**row, "verdict": "not_found", "detail": border, "sidecars_read": len(self.probes)}
        last_old, first_new, unreadable = border
        _, old_side = await self.find(temporal=(None, last_old.start), n=0)
        before, _ = await self.find(temporal=(None, last_old.start), newest_first=True, n=self.near + PICK)
        after, _ = await self.find(temporal=(first_new.start, None), n=self.near + PICK)
        near_old = [g for g in before if g.start <= last_old.start and g.concept_id != last_old.concept_id][: self.near]
        near_new = [g for g in after if g.start >= first_new.start and g.concept_id != first_new.concept_id][
            : self.near
        ]
        (old_near, old_bad), (old_far, old_far_bad), (new_near, new_bad), (new_far, new_far_bad) = await asyncio.gather(
            self.check(near_old, "near", lo[1].kind),
            self.check(await self.spaced(first, last_old, self.spread), "spread", lo[1].kind),
            self.check(near_new, "near", hi[1].kind),
            self.check(await self.spaced(first_new, last, self.spread), "spread", hi[1].kind),
        )
        disagree_old, disagree_new = old_bad + old_far_bad, new_bad + new_far_bad
        return {
            **row,
            "verdict": "step" if not (disagree_old or disagree_new) else "not_a_step",
            "new_version": self.seen[first_new.concept_id].version or hi[1].version,
            "last_old_granule": last_old.ur,
            "last_old_start": cmr_time(last_old.start),
            "first_new_granule": first_new.ur,
            "first_new_start": cmr_time(first_new.start),
            "unreadable_between": unreadable,
            "granules_old_side": old_side,
            "granules_new_side": total - old_side,
            "old_side_checked": old_near + old_far,
            "old_side_disagree": len(disagree_old),
            "new_side_checked": new_near + new_far,
            "new_side_disagree": len(disagree_new),
            "disagreeing": "; ".join(disagree_old + disagree_new),
            "sidecars_read": len(self.probes),
        }


class Live:
    """CMR and the DAAC's distribution endpoint."""

    def __init__(self, session: Any, token: str, per_host: int) -> None:
        from dmrpp.survey import Prober

        self.session, self.prober, self.gate = session, Prober(session, token, per_host), asyncio.Semaphore(4)

    async def search(
        self, collection: str, *, temporal: tuple[datetime | None, datetime | None] | None, newest_first: bool, n: int
    ) -> tuple[list[Granule], int]:
        params = {
            "collection_concept_id": collection,
            "sort_key": "-start_date" if newest_first else "start_date",
            "page_size": str(n),
        }
        if temporal is not None:
            params["temporal"] = f"{cmr_time(temporal[0])},{cmr_time(temporal[1])}"
        import aiohttp

        # CMR sometimes never answers a search that it answers in a tenth of a
        # second when asked again, so give up on each try early and ask again
        timeout = aiohttp.ClientTimeout(total=CMR_TIMEOUT_S if n <= PICK * 3 else CMR_LIST_TIMEOUT_S)
        async with self.gate:
            for attempt in range(CMR_TRIES):
                try:
                    async with self.session.get(
                        CMR, params=params, headers={"User-Agent": USER_AGENT}, timeout=timeout
                    ) as response:
                        if response.status == 200:
                            items = (await response.json()).get("items", [])
                            granules = [g for g in map(granule_of, items) if g is not None]
                            return granules, int(response.headers["CMR-Hits"])
                        if response.status < 500 and response.status != 429:
                            raise RuntimeError(f"CMR answered {response.status}: {(await response.text())[:200]}")
                except (TimeoutError, OSError):  # aiohttp's connection errors are OSErrors
                    pass
                await asyncio.sleep(min(2**attempt, 10))
        raise RuntimeError(f"CMR did not answer a search of {collection} in {CMR_TRIES} tries")

    async def sidecar(self, granule: Granule) -> Sidecar:
        return read_sidecar(await self.prober.stat(granule.sidecar_url, SIDECAR_PREFIX_BYTES))


def chosen(args: argparse.Namespace) -> list[dict[str, str]]:
    names = set(args.short_name or [])
    if args.int16_findings:
        with Path(args.int16_findings).open() as f:
            names |= {r["short_name"] for r in csv.DictReader(f) if int(r["int8_declared_int16"] or 0) > 0}
    with Path(args.collections).open() as f:
        rows = [r for r in csv.DictReader(f) if r["short_name"] in names]
    missing = names - {r["short_name"] for r in rows}
    if missing:
        raise SystemExit(f"not in {args.collections}: {', '.join(sorted(missing))}")
    return sorted(rows, key=lambda r: r["short_name"])


async def run(args: argparse.Namespace) -> int:
    import aiohttp

    token = os.environ.get("EARTHDATA_TOKEN")
    if not token:
        print("EARTHDATA_TOKEN is not set", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    borders, probes = out / "borders.csv", out / "probes.jsonl"
    done = set()
    if borders.exists():
        with borders.open() as f:
            done = {r["concept_id"] for r in csv.DictReader(f)}
    todo = [c for c in chosen(args) if c["concept_id"] not in done]
    print(f"{len(todo)} collections to search ({len(done)} already done)", file=sys.stderr)
    if not borders.exists():
        with borders.open("w", newline="") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writeheader()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        live = Live(session, token, args.per_host)
        gate = asyncio.Semaphore(args.collections_in_flight)

        async def one(collection: dict[str, str]) -> None:
            async with gate:
                search = Search(live, collection, args.near, args.spread)
                try:
                    row = await search.run()
                except Exception as error:  # noqa: BLE001 -- one collection's failure must not end the run
                    row = {
                        "concept_id": collection["concept_id"],
                        "short_name": collection["short_name"],
                        "verdict": "error",
                        "detail": f"{type(error).__name__}: {error}"[:300],
                        "sidecars_read": len(search.probes),
                    }
            with probes.open("a") as f:
                f.writelines(json.dumps(p) + "\n" for p in search.probes)
            with borders.open("a", newline="") as f:
                csv.DictWriter(f, fieldnames=FIELDS).writerow(row)
            print(f"{row['short_name']}: {row['verdict']}", file=sys.stderr, flush=True)

        await asyncio.gather(*(one(c) for c in todo))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--collections", default=COLLECTIONS, help="the survey's collections.csv: short_name to concept_id")
    p.add_argument("--short-name", action="append", help="a collection to search; may be repeated")
    p.add_argument(
        "--int16-findings", help="a verdicts.csv: search every collection with a signed 8-bit variable declared Int16"
    )
    p.add_argument(
        "--out", default=RESULTS, help="borders.csv (one row per collection; a rerun skips those) and probes.jsonl"
    )
    p.add_argument("--near", type=int, default=5, help="granules next to the border to check on each side")
    p.add_argument("--spread", type=int, default=10, help="granules spaced evenly in time to check on each side")
    p.add_argument("--per-host", type=int, default=4, help="sidecar reads in flight per DAAC host")
    p.add_argument("--collections-in-flight", type=int, default=4)
    args = p.parse_args(argv)
    if not (args.short_name or args.int16_findings):
        p.error("give --short-name or --int16-findings")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
