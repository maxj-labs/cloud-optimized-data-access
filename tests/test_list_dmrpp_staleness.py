"""Offline test for the listing proof of concept's pairing."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

spec = importlib.util.spec_from_file_location("list_dmrpp_staleness", Path(__file__).parents[1] / "scripts" / "list_dmrpp_staleness.py")
listing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(listing)

T0 = datetime(2024, 7, 17, tzinfo=UTC)


def obj(path, minutes):
    return {"path": path, "last_modified": T0 + timedelta(minutes=minutes), "size": 10}


def test_pairs_files_with_sidecars_and_finds_the_ones_written_later():
    objects = [
        obj("c/a.nc", 0), obj("c/a.nc.dmrpp", 1),          # sidecar built after the file: fresh
        obj("c/b.nc", 600), obj("c/b.nc.dmrpp", 1),        # file rewritten ten hours later: stale
        obj("c/c.h5", 0),                                  # no sidecar
        obj("c/d.nc.dmrpp", 0),                            # sidecar with no file
        obj("c/e.NC4", 1), obj("c/e.NC4.dmrpp", 0.5),      # 30 s newer: inside the tolerance
        obj("c/readme.txt", 0),
    ]
    out = listing.pair(objects, tolerance_s=60)
    assert (out["objects"], out["data_files"], out["with_sidecar"], out["without_sidecar"], out["orphan_sidecars"]) == (9, 4, 3, 1, 1)
    assert [r["path"] for r in out["stale"]] == ["c/b.nc"]
    assert out["stale"][0]["data_newer_by_s"] == 599 * 60
