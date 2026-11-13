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


def meta(shape=(1, 4, 4), dtype="Float32(endianness='little')", chunks=(1, 2, 2), filters=("shuffle", "deflate")):
    return {"shape": shape, "dtype": dtype, "chunks": chunks, "filters": list(filters)}


def test_metadata_that_changes_what_the_bytes_mean_is_a_mismatch_but_filter_order_is_only_counted():
    same, reordered = verify.compare_metadata({"/a": meta(filters=("deflate", "shuffle"))}, {"/a": meta()})
    assert same == [] and reordered == 1
    differences, _ = verify.compare_metadata(
        {"/a": meta(dtype="Float64(endianness='little')"), "/b": meta()}, {"/a": meta(dtype="Float64(endianness='big')", filters=()), "/b": meta()}
    )
    assert len(differences) == 2 and "/a dtype" in differences[0] and "/a filters" in differences[1]
    index = {"/a": {"0": chunk(0, 10)}}
    out = verify.compare(index, index, 100, {"/a": meta(shape=(1, 8, 4))}, {"/a": meta()})
    assert out["verdict"] == "METADATA_MISMATCH" and "/a shape" in out["metadata_differences"]
    assert verify.compare(index, index, 100, {"/a": meta(filters=("deflate", "shuffle"))}, {"/a": meta()})["verdict"] == "match"


def test_filters_are_named_in_order_without_the_bytes_codec():
    codecs = [SimpleNamespace(codec_name="bytes"), SimpleNamespace(codec_config={"id": "shuffle"}), SimpleNamespace(codec_config={"id": "zlib", "level": 5})]
    assert verify.filters(SimpleNamespace(codecs=codecs)) == ["shuffle", "deflate"]


def test_value_check_picks_mismatched_variables_first_and_skips_large_or_non_numeric_ones():
    file_meta = {
        "/big": meta(shape=(1000, 1000, 100)), "/sst": meta(shape=(1, 100, 100)), "/lat": meta(shape=(100,), dtype="Float64(endianness='big')"),
        "/name": meta(shape=(10,), dtype="FixedLengthUTF32(length=4)"), "/scalar": meta(shape=()),
    }
    assert verify.choose_variables(file_meta, ["/lat"], limit=2, max_bytes=64 * 2**20) == ["/lat", "/sst"]
    assert verify.choose_variables(file_meta, [], limit=5, max_bytes=64 * 2**20) == ["/sst", "/lat"]


def test_same_values_treats_nan_as_equal_and_shape_as_part_of_the_answer():
    import numpy as np

    a = np.array([1.0, np.nan])
    assert verify.same_values(a, a.copy()) and not verify.same_values(a, np.array([1.0, 2.0]))
    assert not verify.same_values(np.zeros((2, 1)), np.zeros(2))
    assert verify.same_values(np.array([1, 2], dtype="int16"), np.array([1, 2], dtype="int32"))
