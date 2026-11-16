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
    assert verify.choose_variables(file_meta, ["/lat"], limit=2) == ["/lat", "/big"]  # no size limit: a block is read, not the whole


def test_the_value_check_reads_a_block_from_the_middle_of_each_variable():
    block = verify.slab((1, 9000, 18000), edge=256)
    assert block == (slice(0, 1), slice(4372, 4628), slice(8872, 9128))
    assert verify.slab((10,), edge=256) == (slice(0, 10),)
    assert verify.constraint("/sea_ice_fraction", block) == "/sea_ice_fraction[0:1:0][4372:1:4627][8872:1:9127]"


def test_same_values_treats_nan_as_equal_and_shape_as_part_of_the_answer():
    import numpy as np

    a = np.array([1.0, np.nan])
    assert verify.same_values(a, a.copy()) and not verify.same_values(a, np.array([1.0, 2.0]))
    assert not verify.same_values(np.zeros((2, 1)), np.zeros(2))
    assert verify.same_values(np.array([1, 2], dtype="int16"), np.array([1, 2], dtype="int32"))


def test_groups_are_listed_and_metadata_mismatches_are_value_checked_before_other_variables():
    array = SimpleNamespace(manifest=SimpleNamespace(dict=lambda: {"0": chunk(1, 2)}))
    leaf = SimpleNamespace(arrays={"v": array}, groups={})
    root = SimpleNamespace(arrays={}, groups={"data_01": SimpleNamespace(arrays={"t": array}, groups={"ku": leaf})})
    assert verify.group_paths(root) == ["/data_01", "/data_01/ku"]
    assert set(verify.manifests(root.groups["data_01"], "/data_01", recurse=False)) == {"/data_01/t"}
    index = {"/sst": {"0": chunk(0, 10)}, "/mask": {"0": chunk(10, 10)}}
    described = {"/sst": meta(), "/mask": meta(dtype="Int8()")}
    out = verify.compare(index, index, 100, {"/sst": meta(), "/mask": meta(dtype="Int16(endianness='little')")}, described)
    assert out["verdict"] == "METADATA_MISMATCH" and out["mismatched_variables"] == ["/mask"]
    assert "/mask dtype: sidecar Int16 little, file Int8" in out["metadata_differences"]


def test_sidecar_signs_are_read_from_the_text():
    old = b'<Dataset name="a.nc"><Int16 name="mask"><Attribute name="fullnamepath" type="String"/></Int16><Attribute name="HDF5_GLOBAL" type="Container"/></Dataset>'
    new = b'<Dataset name="a.nc" dmrpp:version="3.20.9-91"><Int8 name="mask"/><Int8 name="ice"/></Dataset>'
    assert verify.sidecar_signs(old) == {"sidecar_version": "", "sidecar_cf_markers": 1, "sidecar_hdf5_global": True, "sidecar_int8_variables": 0}
    assert verify.sidecar_signs(new) == {"sidecar_version": "3.20.9-91", "sidecar_cf_markers": 0, "sidecar_hdf5_global": False, "sidecar_int8_variables": 2}


def test_pick_without_a_gap_filter_keeps_files_older_than_their_sidecar():
    rows = [{"concept_id": "C1", "data_newer_by_s": "-9", "granule_ur": "fresh"}, {"concept_id": "C2", "data_newer_by_s": "", "granule_ur": "unknown"}]
    assert len(verify.pick(rows, per_collection=1, min_gap_days=None, limit=None)) == 2
    assert verify.pick(rows, per_collection=1, min_gap_days=0, limit=None) == [rows[1]]


def test_flattened_sidecar_names_are_matched_to_grouped_file_names():
    file_names = {"/data_01/time", "/data_01/ku/range", "/lat"}
    assert verify.unflatten({"/data_01_time", "/data_01_ku_range", "/other"}, file_names) == {
        "/data_01_time": "/data_01/time", "/data_01_ku_range": "/data_01/ku/range",
    }
    assert verify.unflatten({"/data_01/time"}, file_names) == {}  # names already shared: nothing to do
    assert verify.unflatten({"/lat_x"}, {"/lat"}) == {}


def test_an_unfiltered_chunk_stored_whole_for_a_smaller_array_is_not_a_mismatch():
    sidecar_meta = {"/time": meta(shape=(1,), dtype="Float64(endianness='little')", chunks=(512,), filters=())}
    file_meta = {"/time": meta(shape=(1,), dtype="Float64(endianness='little')", chunks=(1,), filters=())}
    out = verify.compare({"/time": {"0": chunk(49243, 4096)}}, {"/time": {"0": chunk(49243, 8)}}, 10**6, sidecar_meta, file_meta)
    assert out["verdict"] == "match" and out["chunks_mismatched"] == 0
    moved = verify.compare({"/time": {"0": chunk(100, 4096)}}, {"/time": {"0": chunk(49243, 8)}}, 10**6, sidecar_meta, file_meta)
    assert moved["verdict"] == "MISMATCH"
    packed = {"/time": dict(sidecar_meta["/time"], filters=["deflate"])}
    assert verify.compare({"/time": {"0": chunk(49243, 4096)}}, {"/time": {"0": chunk(49243, 8)}}, 10**6, packed, file_meta)["verdict"] == "MISMATCH"


SIDECAR_XML = b"""<Dataset xmlns="http://xml.opendap.org/ns/DAP/4.0#" xmlns:dmrpp="http://xml.opendap.org/dap/dmrpp/1.0.0#" name="f.hdf">
  <Group name="Grid">
    <Float64 name="Latitude">
      <dmrpp:chunks byteOrder="LE"><dmrpp:chunk offset="8068541" nBytes="13572" href="OPeNDAP_DMRpp_SC_DATA_ACCESS_URL"/></dmrpp:chunks>
    </Float64>
    <Group name="Data_Fields">
      <Int16 name="ET"><dmrpp:chunks byteOrder="BE"><dmrpp:chunk offset="3622" nBytes="32"/></dmrpp:chunks></Int16>
      <Byte name="QC"><Attribute name="_FillValue" type="String"><Value>NA</Value></Attribute><dmrpp:chunks byteOrder="BE"><dmrpp:chunk offset="20398" nBytes="26"/></dmrpp:chunks></Byte>
    </Group>
  </Group>
</Dataset>"""


def test_sidecar_groups_byte_order_and_external_chunks_are_read_from_the_xml():
    from xml.etree import ElementTree

    root = ElementTree.fromstring(SIDECAR_XML)
    assert verify.sidecar_group_paths(root) == ["/Grid", "/Grid/Data_Fields"]
    notes = verify.sidecar_notes(root)
    assert notes == {
        "/Grid/Latitude": {"big_endian": False, "external": True, "text_fill": False},
        "/Grid/Data_Fields/ET": {"big_endian": True, "external": False, "text_fill": False},
        "/Grid/Data_Fields/QC": {"big_endian": True, "external": False, "text_fill": True},
    }
    index = {"/Grid/Latitude": {"0.0": chunk(8068541, 13572)}, "/Grid/Data_Fields/ET": {"0.0": chunk(3622, 32)}}
    meta = {
        "/Grid/Latitude": {"dtype": "Float64(endianness='little')"},
        "/Grid/Data_Fields/ET": {"dtype": "Int16(endianness='little')"},
        "/Grid/Data_Fields/QC": {"dtype": "UInt8()"},
    }
    assert verify.apply_sidecar_notes(index, meta, notes) == 1
    assert "/Grid/Latitude" not in index and "/Grid/Latitude" not in meta
    assert meta["/Grid/Data_Fields/ET"]["dtype"] == "Int16(endianness='big')"
    assert meta["/Grid/Data_Fields/QC"]["dtype"] == "UInt8()"


def test_hdf4_names_are_matched_by_a_last_name_unique_on_both_sides():
    sidecar = {"/G1/Data_Fields/ET", "/G1/Data_Fields/QC", "/G2/Data_Fields/QC", "/G1/Data_Fields/Cloud_Top", "/inlined"}
    file = {"/ET", "/QC", "/Cloud Top"}
    assert verify.match_by_leaf(sidecar, file) == {"/G1/Data_Fields/ET": "/ET", "/G1/Data_Fields/Cloud_Top": "/Cloud Top"}
    assert verify.match_by_leaf({"/ET"}, {"/ET"}) == {}


def test_hdf4_value_check_asks_hyrax_only_for_variables_with_a_sidecar_name():
    meta = {"/ET": {"dtype": "Int16"}, "/QC": {"dtype": "UInt16"}}
    assert verify.value_candidates("HDF4", meta, {"/ET": "/Grid/Data_Fields/ET"}) == {"/ET": {"dtype": "Int16"}}
    assert verify.value_candidates("HDF5", meta, {}) == meta
