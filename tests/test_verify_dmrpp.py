"""Offline tests for the DMR++ verifier's comparison and sampling."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location("verify_dmrpp", Path(__file__).parents[1] / "scripts" / "verify_dmrpp.py")
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


def chunk(offset, length):
    return {"path": "s3://b/f.nc", "offset": offset, "length": length}


def test_matching_indexes_match_and_extra_variables_are_only_counted():
    file = {"/a": {"0.0": chunk(100, 50), "0.1": chunk(150, 50)}, "/small": {"0": chunk(10, 4)}}
    sidecar = {"/a": {"0.0": chunk(100, 50), "0.1": chunk(150, 50)}, "/inlined": {}}
    out = verify.compare(sidecar, file, file_size=1000)
    assert out["verdict"] == "match" and out["chunks_compared"] == 2 and out["chunks_mismatched"] == 0
    assert out["only_in_file"] == 1 and out["only_in_sidecar"] == 1


def test_a_moved_chunk_is_a_mismatch_with_an_example():
    file = {"/a": {"0.0": chunk(100, 50), "0.1": chunk(150, 50)}}
    sidecar = {"/a": {"0.0": chunk(100, 50), "0.1": chunk(160, 50)}}
    out = verify.compare(sidecar, file, file_size=1000)
    assert out["verdict"] == "MISMATCH" and out["chunks_mismatched"] == 1 and out["variables_mismatched"] == 1
    assert out["first_mismatch"] == "/a[0.1]: sidecar 160+50, file 150+50"


def test_a_sidecar_pointing_past_the_end_of_the_file_is_its_own_verdict():
    index = {"/a": {"0": chunk(900, 200)}}
    assert verify.compare(index, index, file_size=1000)["verdict"] == "SIDECAR_PAST_END"
    assert verify.compare(index, index, file_size=1100)["verdict"] == "match"
    assert verify.compare({"/a": {}}, {"/b": {}}, file_size=10)["verdict"] == "nothing_to_compare"


def test_manifests_walks_nested_groups():
    array = SimpleNamespace(manifest=SimpleNamespace(dict=lambda: {"0": chunk(1, 2)}))
    inner = SimpleNamespace(arrays={"v": array}, groups={})
    root = SimpleNamespace(arrays={"t": array}, groups={"product": inner})
    assert set(verify.manifests(root)) == {"/t", "/product/v"}


def test_pick_takes_a_few_per_collection_largest_gaps_first():
    rows = [{"concept_id": "C1", "data_newer_by_s": str(86400 * d), "granule_ur": f"a{d}"} for d in (1, 2, 3)]
    rows += [{"concept_id": "C2", "data_newer_by_s": str(86400 * 400), "granule_ur": "b"}]
    rows += [{"concept_id": "C3", "data_newer_by_s": "600", "granule_ur": "c"}]
    chosen = verify.pick(rows, per_collection=2, min_gap_days=0.5, limit=None)
    assert chosen[0]["granule_ur"] == "b" and len(chosen) == 3 and all(r["concept_id"] != "C3" for r in chosen)
    assert len(verify.pick(rows, per_collection=2, min_gap_days=0, limit=2)) == 2


def test_the_listing_scripts_rows_are_accepted():
    row = verify.normalise({"concept_id": "C1-POCLOUD", "short_name": "X", "bucket": "b", "path": "X/f.nc", "data_newer_by_s": "90000"})
    assert row["data_url"] == "s3://b/X/f.nc" and row["provider"] == "POCLOUD"
    assert verify.pick([row], per_collection=1, min_gap_days=1, limit=None) == [row]
    kept = verify.normalise({"provider": "P", "data_url": "https://h/f.nc", "concept_id": "C2-Q"})
    assert kept["data_url"] == "https://h/f.nc" and kept["provider"] == "P"
