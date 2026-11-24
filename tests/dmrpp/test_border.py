"""Offline tests for the sidecar border search, against a made-up archive."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from dmrpp import border

T0 = datetime(2010, 1, 1, tzinfo=UTC)
COLLECTION = {"concept_id": "C1-TEST", "short_name": "TEST"}


class FakeArchive:
    """Granules one hour apart and an hour long. ``kinds[i]`` is granule i's sidecar."""

    def __init__(self, kinds: list[str], length: timedelta = timedelta(hours=1)) -> None:
        self.kinds, self.length = kinds, length
        self.granules = [
            border.Granule(f"G{i}", f"granule-{i}", T0 + timedelta(hours=i), f"https://x/{i}.nc.dmrpp")
            for i in range(len(kinds))
        ]
        self.reads: list[str] = []
        self.searches = 0

    async def search(self, collection, *, temporal, newest_first, n):
        self.searches += 1
        after, before = temporal or (None, None)
        found = [
            g
            for g in self.granules
            if (before is None or g.start <= before) and (after is None or g.start + self.length >= after)
        ]
        if newest_first:
            found.reverse()
        return found[:n], len(found)

    async def sidecar(self, granule):
        self.reads.append(granule.concept_id)
        kind = self.kinds[int(granule.concept_id[1:])]
        return border.Sidecar(kind, "3.21.1-367" if kind == "versioned" else None)


def search(kinds, near=3, spread=4, **kwargs):
    archive = FakeArchive(kinds, **kwargs)
    s = border.Search(archive, COLLECTION, near, spread)
    return asyncio.run(s.run()), archive, s


def test_a_clean_border_is_found_exactly_and_called_a_step():
    row, archive, s = search(["unversioned"] * 13_337 + ["versioned"] * 6_663)
    assert row["verdict"] == "step"
    assert (row["last_old_granule"], row["first_new_granule"]) == ("granule-13336", "granule-13337")
    assert (row["granules"], row["granules_old_side"], row["granules_new_side"]) == (20_000, 13_337, 6_663)
    assert (row["old_kind"], row["new_kind"], row["new_version"]) == ("unversioned", "versioned", "3.21.1-367")
    assert row["unreadable_between"] == 0
    # three next to the border and four spread out, on each side
    assert (row["old_side_checked"], row["new_side_checked"]) == (7, 7)
    assert (row["old_side_disagree"], row["new_side_disagree"]) == (0, 0)
    # the bisection had already read some of the neighbours; each sidecar is read once
    read = {int(p["granule_ur"].split("-")[1]): p["role"] for p in s.probes}
    assert set(range(13_333, 13_341)) <= set(read)
    assert {i for i, role in read.items() if role == "near"} <= {13_333, 13_334, 13_335, 13_338, 13_339, 13_340}
    # a bisection, not a scan: log2(20,000) is about 14
    assert len(archive.reads) < 45 and archive.searches < 60
    assert row["sidecars_read"] == len(archive.reads) == len(set(archive.reads))


def test_the_border_can_be_at_either_edge_of_the_collection():
    row, _, _ = search(["unversioned"] + ["versioned"] * 2_999)
    assert (row["verdict"], row["granules_old_side"], row["first_new_granule"]) == ("step", 1, "granule-1")
    row, _, _ = search(["unversioned"] * 2_999 + ["versioned"])
    assert (row["verdict"], row["granules_old_side"], row["last_old_granule"]) == ("step", 2_999, "granule-2998")


def test_an_island_of_the_other_kind_on_one_side_is_not_a_step():
    # old sidecars again in the second half of the new side: reprocessing out of order
    kinds = ["unversioned"] * 4_000 + ["versioned"] * 2_000 + ["unversioned"] * 1_500 + ["versioned"] * 500
    row, _, _ = search(kinds, spread=8)
    assert row["verdict"] == "not_a_step"
    assert row["old_side_disagree"] + row["new_side_disagree"] > 0
    assert "is unversioned" in row["disagreeing"] or "is versioned" in row["disagreeing"]


def test_an_exception_next_to_the_border_is_not_a_step():
    kinds = ["unversioned"] * 3_000 + ["versioned"] * 3_000
    kinds[2_998] = "versioned"
    row, _, _ = search(kinds)
    assert row["verdict"] == "not_a_step"
    assert row["old_side_disagree"] == 1 and "granule-2998" in row["disagreeing"]


def test_ends_that_agree_are_checked_across_the_whole_collection():
    row, archive, _ = search(["unversioned"] * 5_000)
    assert (row["verdict"], row["old_side_checked"], row["old_side_disagree"]) == ("all_unversioned", 8, 0)
    assert "last_old_granule" not in row
    row, _, _ = search(["versioned"] * 2_000 + ["unversioned"] * 1_000 + ["versioned"] * 2_000)
    assert row["verdict"] == "mixed" and row["old_side_disagree"] > 0


def test_granules_without_a_sidecar_are_stepped_over():
    kinds = ["unversioned"] * 1_000 + ["none"] * 7 + ["versioned"] * 1_000
    kinds[0] = kinds[-1] = "none"  # the ends too
    kinds[500] = "unreadable"
    row, _, _ = search(kinds)
    assert row["verdict"] == "step"
    assert (row["last_old_granule"], row["first_new_granule"]) == ("granule-999", "granule-1007")
    assert row["unreadable_between"] == 7


def test_granules_longer_than_their_spacing_do_not_move_the_border():
    # each granule overlaps the next 30: a temporal search returns many that start before its window
    row, _, _ = search(["unversioned"] * 4_321 + ["versioned"] * 3_000, length=timedelta(hours=30))
    assert (row["verdict"], row["last_old_granule"], row["first_new_granule"]) == (
        "step",
        "granule-4320",
        "granule-4321",
    )
    assert row["granules_old_side"] == 4_321


def test_a_collection_with_no_sidecars_says_so():
    row, _, _ = search(["none"] * 50)
    assert row["verdict"] == "no_readable_sidecar"


def test_reading_a_sidecar_prefix():
    head = b'<?xml version="1.0" encoding="ISO-8859-1"?>\n<Dataset xmlns="http://xml.opendap.org/ns/DAP/4.0#"'

    def stat(status, body=b""):
        return {"status": status, "body": body, "last_modified": "2024-07-17T11:24:22+00:00", "size": 1}

    assert border.read_sidecar(stat(206, head + b' dmrpp:version="3.20.13-310">')).kind == "versioned"
    assert border.read_sidecar(stat(206, head + b' dmrpp:version="3.20.13-310">')).version == "3.20.13-310"
    assert border.read_sidecar(stat(206, head + b' name="a.nc">')) == border.Sidecar(
        "unversioned", None, "2024-07-17T11:24:22+00:00", 206
    )
    assert border.read_sidecar(stat(404)).kind == "none"
    assert border.read_sidecar(stat(200, b"<!DOCTYPE html><html>404 - Not Found</html>")).kind == "none"
    assert border.read_sidecar(stat("login-redirect")).kind == "unreadable"


def test_granules_and_times_from_cmr():
    item = {
        "meta": {"concept-id": "G1-TEST"},
        "umm": {
            "GranuleUR": "a",
            "TemporalExtent": {"RangeDateTime": {"BeginningDateTime": "2006-06-12T12:00:00.250Z"}},
            "RelatedUrls": [{"URL": "https://d.example.nasa.gov/a.nc", "Type": "GET DATA"}],
        },
    }
    granule = border.granule_of(item)
    assert granule.sidecar_url == "https://d.example.nasa.gov/a.nc.dmrpp"
    assert border.cmr_time(granule.start) == "2006-06-12T12:00:00.250Z" and border.cmr_time(None) == ""
    item["umm"]["TemporalExtent"] = {"SingleDateTime": "2006-06-12T12:00:00Z"}
    assert border.cmr_time(border.granule_of(item).start) == "2006-06-12T12:00:00.000Z"
    item["umm"]["TemporalExtent"] = {}
    assert border.granule_of(item) is None
