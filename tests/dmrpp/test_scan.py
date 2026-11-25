"""Offline tests for the sidecar scan, against an in-memory object store."""

from __future__ import annotations

import asyncio
import csv

import obstore
from obstore.store import MemoryStore

from dmrpp import scan

HEAD = b'<?xml version="1.0" encoding="ISO-8859-1"?>\n<Dataset xmlns="http://xml.opendap.org/ns/DAP/4.0#"'
MARK = b'<Int16 name="mask"><Attribute name="fullnamepath" type="String"><Value>/mask</Value></Attribute>'
FLAGGED = HEAD + b' name="a.nc">' + MARK + b"x" * 20_000
PLAIN = HEAD + b' name="a.nc"><Int8 name="mask"/>'
VERSIONED = HEAD + b' dmrpp:version="3.20.9-91" name="a.nc"><Int8 name="mask"/>'


def archive(count: int = 2_500) -> MemoryStore:
    """A collection of ``count`` granules: the first 40% flagged, then 10% unversioned but clear, then versioned."""
    store = MemoryStore()
    for i in range(count):
        body = FLAGGED if i < count * 0.4 else PLAIN if i < count * 0.5 else VERSIONED
        obstore.put(store, f"COLL/g{i:05d}.nc", b"\x89HDF")
        obstore.put(store, f"COLL/g{i:05d}.nc.dmrpp", body)
        obstore.put(store, f"COLL/g{i:05d}.nc.md5", b"0")
    obstore.put(store, "COLL/broken.nc.dmrpp", b"<html>not found</html>")
    obstore.put(store, "OTHER/g.nc.dmrpp", FLAGGED)
    return store


def read(path):
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def test_every_sidecar_under_the_prefix_gets_one_row(tmp_path):
    store, rows = archive(), tmp_path / "COLL.csv"
    asyncio.run(scan.scan_prefix(lambda: store, "COLL/", rows))
    found = read(rows)
    assert len(found) == 2_501 and all(r["path"].startswith("COLL/") and r["path"].endswith(".dmrpp") for r in found)
    assert [r["path"] for r in found] == sorted(r["path"] for r in found)
    assert scan.summarize(rows) == {
        "sidecars": 2_501,
        "flagged": 1_000,
        "clear": 1_500,
        "clear_unversioned": 250,
        "not_a_sidecar": 1,
        "unreadable": 0,
        "flagged_last_modified_from": min(r["last_modified"] for r in found if r["kind"] == "flagged"),
        "flagged_last_modified_to": max(r["last_modified"] for r in found if r["kind"] == "flagged"),
    }
    by_path = {r["path"]: r for r in found}
    assert by_path["COLL/g00000.nc.dmrpp"]["fullnamepath"] == "True"
    assert by_path["COLL/g02499.nc.dmrpp"]["dmrpp_version"] == "3.20.9-91"
    assert int(by_path["COLL/g00000.nc.dmrpp"]["size"]) == len(FLAGGED)


def test_new_credentials_mid_listing_lose_nothing_and_repeat_nothing(tmp_path):
    store, rows, stores = archive(), tmp_path / "COLL.csv", []

    def get_store():
        stores.append(1)
        return store

    # a store is "too old" after every page of the listing
    asyncio.run(scan.scan_prefix(get_store, "COLL/", rows, renew_after_s=-1))
    paths = [r["path"] for r in read(rows)]
    assert len(stores) > 5 and len(paths) == len(set(paths)) == 2_501


def test_an_unfinished_scan_is_taken_up_after_its_last_row(tmp_path):
    store, rows, whole = archive(), tmp_path / "COLL.csv", tmp_path / "whole.csv"
    asyncio.run(scan.scan_prefix(lambda: store, "COLL/", whole))
    lines = whole.read_text().splitlines(keepends=True)
    rows.write_text("".join(lines[:701]))  # the header and 700 sidecars, as a stopped run leaves it
    asyncio.run(scan.scan_prefix(lambda: store, "COLL/", rows))
    assert rows.read_text() == whole.read_text()


def test_a_sidecar_that_cannot_be_read_is_a_row(tmp_path, monkeypatch):
    store, rows = archive(30), tmp_path / "COLL.csv"
    real = obstore.get_range_async

    async def failing(store, path, **kwargs):
        if path.endswith("g00007.nc.dmrpp"):
            raise OSError("reset")
        return await real(store, path, **kwargs)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(obstore, "get_range_async", failing)
    monkeypatch.setattr(scan.asyncio, "sleep", no_sleep)
    asyncio.run(scan.scan_prefix(lambda: store, "COLL/", rows))
    assert {r["path"]: r["kind"] for r in read(rows)}["COLL/g00007.nc.dmrpp"] == "error: OSError"
    assert scan.summarize(rows)["unreadable"] == 1


def test_the_command_writes_a_summary_and_skips_what_is_done(tmp_path, monkeypatch, capsys):
    from dmrpp import list_collection

    store = archive(300)
    collections = tmp_path / "collections.csv"
    collections.write_text("concept_id,short_name,provider\nC1-POCLOUD,COLL,POCLOUD\nC2-POCLOUD,GONE,POCLOUD\n")
    asked = []

    class Stores:
        def __init__(self, region, anonymous):
            pass

        def get(self, bucket, provider):
            asked.append((bucket, provider))
            return store

    def prefix_of(concept_id, up):
        if concept_id == "C2-POCLOUD":
            raise RuntimeError("CMR lists no s3:// link")
        return "s3://a-bucket/COLL/"

    monkeypatch.setattr(list_collection, "Stores", Stores)
    monkeypatch.setattr(scan, "prefix_of", prefix_of)
    out = tmp_path / "scan"
    args = ["--collections", str(collections), "--short-name", "COLL", "--short-name", "GONE", "--out", str(out)]
    assert scan.main(args) == 0
    summary = {r["short_name"]: r for r in read(out / "summary.csv")}
    assert (summary["COLL"]["sidecars"], summary["COLL"]["flagged"], summary["COLL"]["clear"]) == ("301", "120", "180")
    assert summary["COLL"]["prefix"] == "s3://a-bucket/COLL/" and not summary["COLL"]["error"]
    assert "no s3:// link" in summary["GONE"]["error"]
    assert asked == [("a-bucket", "POCLOUD")]
    import gzip

    with gzip.open(out / "sidecars" / "COLL.csv.gz", "rt") as f:
        assert len(f.read().splitlines()) == 302
    assert not (out / "sidecars" / "COLL.csv").exists()
    # a rerun scans only the collection that failed
    asked.clear()
    assert scan.main(args) == 0
    assert asked == [] and len(read(out / "summary.csv")) == 2
    assert "1 collections to scan (1 already done)" in capsys.readouterr().err
