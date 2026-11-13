"""Offline tests: the arms on synthetic TEMPO-shaped granules and a local
virtual store, the question geometry, the gate, the plan, the report and the
superchunk arithmetic."""

from __future__ import annotations

import json

import numpy as np
import pytest

from benchmarks.tempo_no2 import cases as tempo_cases
from benchmarks.trade_study import report, run, scope
from benchmarks.trade_study import superchunks as sc
from benchmarks.trade_study.arms import ARM_CLASSES, Result
from benchmarks.trade_study.datasets import (
    Case,
    Goes19,
    Selection,
    TempoNo2,
    goes_scan_angles,
    headline_cases,
    parse_case,
    question_points,
)
from benchmarks.trade_study.trial import Row, answer, price, run_trial
from tests.tempo_no2 import fixtures

ARMS = ("tuned_h5py", "user_stack", "download", "virtual_icechunk")
GOES_GRID = {
    "x": {"scale": 5.6e-05, "offset": -0.151844, "size": 5424}, "y": {"scale": -5.6e-05, "offset": 0.151844, "size": 5424},
    "perspective_point_height": 35786023.0, "semi_major_axis": 6378137.0, "semi_minor_axis": 6356752.31414,
    "longitude_of_projection_origin": -75.0,
}


class FixtureTempo(TempoNo2):
    """TEMPO's layout on local files: no credentials, a local repository."""

    public = True

    def __init__(self, repo):
        self._repo = repo

    def auth(self):
        return None

    def store_spec(self, credentials):
        return {"kind": "local"}

    def filesystem(self, credentials):
        import fsspec

        return fsspec.filesystem("file")

    def open_repo(self, settings):
        return self._repo


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("trade_study")
    granules = fixtures.make_granules(tmp / "granules", n=3)
    repo, snapshot, _ = fixtures.make_virtual_store(tmp, granules)
    lat, lon = np.linspace(20.0, 50.0, 60), np.linspace(-120.0, -70.0, 80)
    payload = {
        "window": "1d", "snapshot_id": snapshot, "discovery_seconds": 0.5, "index_location": "local-disk",
        "grid": tempo_cases.grid_spec(lat, lon), "granules": fixtures.manifest_for(granules).to_dict(orient="records"),
    }
    return FixtureTempo(repo), payload


def read(name, dataset, payload, case, settings=None):
    arm = ARM_CLASSES[name](dataset, payload, settings)
    observations = dataset.observations(case, len(payload["granules"]))
    arm.authenticate()
    arm.open(observations)
    try:
        return arm.read(observations, dataset.selection(case, payload["grid"]))
    finally:
        arm.close()


# --- scope ------------------------------------------------------------------------


def test_every_built_arm_has_a_class_and_every_class_is_registered():
    assert set(scope.RUNNABLE) == set(ARM_CLASSES)


def test_status_lists_every_arm_dataset_workload_and_metric():
    text = scope.render_status()
    for name in [a.name for a in scope.ARMS] + [d.name for d in scope.DATASETS] + [w.name for w in scope.WORKLOADS]:
        assert f"`{name}" in text
    assert "first_byte_seconds" in text


def test_a_proposed_arm_is_refused_with_what_it_needs(world):
    with pytest.raises(SystemExit, match="proposed.*HDF5 >= 2.2.0"):
        run.make_arm("ros3", *world)


# --- questions --------------------------------------------------------------------


def test_question_points_are_the_tempo_benchmarks_points_and_nested(lat, lon):
    lats, lons = question_points(250)
    spatial = tempo_cases.prepare_spatial_cases(lat, lon)
    assert np.array_equal(lats, spatial.points["requested_lat"]) and np.array_equal(lons, spatial.points["requested_lon"])
    assert np.array_equal(question_points(10)[0], lats[:10])


def test_goes_projection_puts_the_subsatellite_point_at_the_grid_centre():
    y, x = goes_scan_angles(GOES_GRID, np.array([0.0]), np.array([-75.0]))
    assert abs(y[0]) < 1e-12 and abs(x[0]) < 1e-12
    yi, xi = Goes19().index_points(GOES_GRID, np.array([0.0]), np.array([-75.0]))
    assert abs(yi[0] - 2711.5) <= 0.5 and abs(xi[0] - 2711.5) <= 0.5


def test_goes_north_is_up_and_east_is_right():
    yi, xi = Goes19().index_points(GOES_GRID, np.array([10.0, 0.0]), np.array([-75.0, -65.0]))
    assert yi[0] < 2711 and xi[1] > 2712


def test_goes_box_is_about_the_stated_size():
    ys, xs = Goes19().index_window(GOES_GRID, tempo_cases.aoi_bounds(100))
    # 2 km pixels at nadir, stretched towards the limb: a 100 km box is a few dozen pixels
    assert 20 <= ys.stop - ys.start <= 60 and 20 <= xs.stop - xs.start <= 60


def test_goes_point_off_the_disk_is_an_error():
    with pytest.raises(ValueError, match="off the full-disk grid"):
        Goes19().index_points(GOES_GRID, np.array([0.0]), np.array([20.0]))


def test_first_value_of_a_selection():
    assert Selection("window", slice(5, 9), slice(2, 7)).first() == Selection("window", slice(5, 6), slice(2, 3))
    first = Selection("points", np.array([4, 5]), np.array([6, 7])).first()
    assert first.y.tolist() == [4] and first.x.tolist() == [6]


def test_cases():
    assert parse_case("churro:10:1d") == Case("churro", 10, "1d")
    with pytest.raises(ValueError):
        parse_case("point:10:1d")
    assert [str(c) for c in headline_cases("1d")] == ["pancake:1:1d", "pancake:15:1d", "churro:10:1d", "churro:250:1d", "box:100:1d", "batch:0:1d"]
    assert TempoNo2.observations(Case("pancake", 15, "1d"), 12) == list(range(12))
    assert TempoNo2.observations(Case("pancake", 1, "1d"), 12) == [0]
    assert TempoNo2.observations(Case("batch", 0, "1d"), 12) == list(range(12))


# --- arms -------------------------------------------------------------------------


@pytest.mark.parametrize("case", [Case("churro", 10, "1d"), Case("box", 500, "1d"), Case("pancake", 1, "1d"), Case("batch", 0, "1d")])
def test_every_arm_returns_the_reference_values(world, case):
    dataset, payload = world
    reference = read("tuned_h5py", dataset, payload, case, {"max_workers": 1})
    n_obs = len(dataset.observations(case, 3))
    assert reference.times.shape == (n_obs,)
    assert np.isfinite(reference.values["vertical_column_troposphere"]).any()
    for name in ARMS:
        status, _ = run.compare(reference, read(name, dataset, payload, case))
        assert status == "exact", name


def test_reference_values_are_the_fixture_values(world):
    dataset, payload = world
    got = read("tuned_h5py", dataset, payload, Case("pancake", 1, "1d"))
    expected = fixtures.expected_no2(0, 60, 80).astype(float)
    expected[expected == fixtures.NO2_FILL] = np.nan
    assert np.array_equal(got.values["vertical_column_troposphere"], expected, equal_nan=True)


def test_first_byte_is_the_sum_of_its_parts(world):
    dataset, payload = world
    row = run_trial(ARM_CLASSES["tuned_h5py"](dataset, payload), dataset, Case("box", 500, "1d"), "first_byte")
    assert row.status == "ok", row.error
    assert row.chunk_id_seconds is not None and row.chunk_id_reason is None
    parts = row.auth_seconds + row.manifest_seconds + row.chunk_id_seconds + row.byte_seconds
    assert row.first_byte_seconds == pytest.approx(parts, abs=1e-6)
    assert row.end_to_end_seconds is None and row.last_byte_seconds is None
    assert row.arm_settings["workers"] == "in-process"  # no worker pool before the first value
    assert row.warm_seconds is not None and row.discovery_seconds == 0.5 and row.request_count > 0


@pytest.mark.parametrize("name", ARMS)
def test_full_trial_records_every_part_or_a_reason(world, name):
    dataset, payload = world
    row = run_trial(ARM_CLASSES[name](dataset, payload), dataset, Case("churro", 10, "1d"), "full",
                    probes={"rtt_ms_median": 1.0, "bandwidth_mb_s": 100.0})
    assert row.status == "ok", row.error
    assert row.first_byte_seconds is None
    assert row.end_to_end_seconds >= row.last_byte_seconds > 0
    assert (row.chunk_id_seconds is None) == (row.chunk_id_reason is not None)
    assert row.byte_seconds > 0 and row.observation_count == 3
    assert row.floor_seconds > 0 and row.cost_usd > 0
    assert row.arm_status == "built" and row.section == 1
    assert Row.from_json(row.to_json()).end_to_end_seconds == row.end_to_end_seconds
    if name == "virtual_icechunk":
        assert row.request_count_kind == "static-minimum"


def test_a_failing_arm_is_a_row_not_a_crash(world):
    dataset, payload = world
    broken = {**payload, "granules": [{**g, "source_url": g["source_url"] + ".missing"} for g in payload["granules"]]}
    row = run_trial(ARM_CLASSES["download"](dataset, broken), dataset, Case("pancake", 1, "1d"), "full")
    assert row.status != "ok" and row.error


def test_answer_is_a_nan_aware_mean_per_observation():
    result = Result(np.zeros(2, dtype="datetime64[ns]"), {"v": np.array([[1.0, np.nan, 3.0], [np.nan, np.nan, np.nan]])})
    out = answer(result)["v"]
    assert out[0] == 2.0 and np.isnan(out[1])


def test_transfer_is_free_in_region_and_egress_otherwise():
    inside = Row(measure="full", end_to_end_seconds=3600.0, request_count=1000, net_rx_bytes=10**9, in_region=True)
    outside = Row(measure="full", end_to_end_seconds=3600.0, request_count=1000, net_rx_bytes=10**9, in_region=False)
    price(inside, 0.192)
    price(outside, 0.192)
    assert inside.transfer_usd == 0 and outside.transfer_usd == pytest.approx(0.09)
    assert inside.cost_usd == pytest.approx(0.0004 + 0.192)


# --- gate -------------------------------------------------------------------------


def _result(values):
    return Result(np.array(["2025-03-14T12:00:00"], dtype="datetime64[ns]"), {"v": np.asarray(values, dtype=float)[None]})


def test_compare_exact_close_vacuous_and_failures():
    assert run.compare(_result([1.0, np.nan]), _result([1.0, np.nan])) == ("exact", 0.0)
    assert run.compare(_result([1.0]), _result([1.0 + 1e-8]))[0] == "close"
    assert run.compare(_result([np.nan]), _result([np.nan]))[0] == "vacuous"
    with pytest.raises(AssertionError, match="values differ"):
        run.compare(_result([1.0]), _result([1.001]))
    with pytest.raises(AssertionError, match="fill masks"):
        run.compare(_result([1.0, np.nan]), _result([1.0, 2.0]))
    later = Result(np.array(["2025-03-14T13:00:00"], dtype="datetime64[ns]"), {"v": np.ones((1, 1))})
    with pytest.raises(AssertionError, match="times"):
        run.compare(_result([1.0]), later)


def test_gate_runs_every_arm_against_the_first(world):
    dataset, payload = world
    rows = run.run_gate(dataset, payload, ARMS, [Case("churro", 10, "1d"), Case("box", 500, "1d")])
    assert len(rows) == 2 * 3 * 3  # cases × observations × non-reference arms
    assert run.gate_verdicts(rows, "tuned_h5py") == {"tuned_h5py": "reference", "user_stack": "pass", "download": "pass", "virtual_icechunk": "pass"}


def test_gate_verdicts_fail_and_vacuous():
    rows = [
        {"case": "a", "arm": "x", "status": "exact"}, {"case": "b", "arm": "x", "status": "fail: boom"},
        {"case": "a", "arm": "y", "status": "vacuous"}, {"case": "a", "arm": "z", "status": "close"},
    ]
    assert run.gate_verdicts(rows, "ref") == {"ref": "reference", "x": "fail", "y": "vacuous", "z": "pass"}


# --- plan and report --------------------------------------------------------------


def test_plan_runs_every_cell_once_per_repetition_in_shuffled_order():
    cases = [Case("churro", 10, "1d"), Case("box", 100, "1d")]
    specs = run.plan(("tuned_h5py", "download"), cases, reps=3, variants=[("tuned_h5py", {"max_workers": 1})])
    assert len(specs) == 3 * (2 * 2 * 2 + 1)
    for rep in range(3):
        block = [s for s in specs if s["rep"] == rep]
        assert len({(s["arm"], s["case"], s["measure"], json.dumps(s["settings"])) for s in block}) == len(block)
    assert [s["arm"] for s in specs[:9]] != sorted(s["arm"] for s in specs[:9])
    assert specs == run.plan(("tuned_h5py", "download"), cases, reps=3, variants=[("tuned_h5py", {"max_workers": 1})])


def test_report_summarises_medians_and_keeps_failures(world, tmp_path):
    dataset, payload = world
    rows = []
    for name in ("tuned_h5py", "virtual_icechunk"):
        for measure in ("first_byte", "full"):
            for rep in range(2):
                row = run_trial(ARM_CLASSES[name](dataset, payload), dataset, Case("box", 500, "1d"), measure, rep=rep,
                                probes={"rtt_ms_median": 1.0, "bandwidth_mb_s": 100.0}, warm=False)
                row.run_id, row.gate_status = "t", "pass" if name == "tuned_h5py" else "not gated"
                rows.append(row)
    rows.append(Row(run_id="t", dataset="tempo_no2", arm="download", workload="box", case_size=500, window="1d", measure="full", status="subprocess: timeout"))
    (tmp_path / "results.jsonl").write_text("".join(r.to_json() + "\n" for r in rows))
    text = report.write_report(tmp_path)
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["rows"] == 9 and summary["failed"] == 1 and len(summary["cells"]) == 4
    cell = next(c for c in summary["cells"] if c["arm"] == "tuned_h5py" and c["measure"] == "first_byte")
    assert cell["n"] == 2 and cell["first_byte_seconds"] > 0 and cell["chunk_id_seconds"] is not None
    assert "time to first byte, in parts" in text and "`virtual_icechunk` †" in text and "`tuned_h5py` |" in text
    assert "Failed trials" in text and "≥ " in text


# --- superchunks ------------------------------------------------------------------


def test_group_merges_within_the_gap_and_the_size_cap():
    ranges = [(0, 10), (10, 10), (25, 10), (100, 10)]
    assert sc.group(ranges, 0) == [(0, 20), (25, 10), (100, 10)]
    assert sc.group(ranges, 5) == [(0, 35), (100, 10)]
    assert sc.group(ranges, 1000) == [(0, 110)]
    assert sc.group(ranges, 1000, max_bytes=40) == [(0, 35), (100, 10)]
    assert sc.group([(50, 10), (0, 10)], 0) == [(0, 10), (50, 10)]


def test_analyse_counts_requests_bytes_and_over_read_per_granule():
    out = sc.analyse([(0, 10), (10, 10), (25, 10)], n_granules=4, gaps=(0, 5))
    assert [(g.max_gap, g.requests, g.bytes_read) for g in out] == [(-1, 12, 120), (0, 8, 120), (5, 4, 140)]
    assert out[0].over_read == 0 and out[2].over_read == pytest.approx(140 / 120 - 1)
    # grouping cannot go below one request per file
    assert sc.analyse([(0, 10), (10, 10)], n_granules=7, gaps=(10**9,))[1].requests == 7
    assert sc.floor(out[2], rtt_s=0.1, bandwidth_bps=140.0, concurrency=2) == pytest.approx(2 * 0.1 + 1.0)


def test_needed_ranges_come_from_the_files_own_chunk_index(world):
    import h5py

    dataset, payload = world
    path = payload["granules"][0]["source_url"].removeprefix("file://")
    with h5py.File(path, "r") as handle:
        everything = sc.needed_ranges(handle, dataset.variables, dataset.selection(Case("pancake", 1, "1d"), payload["grid"]))
        one = sc.needed_ranges(handle, dataset.variables, dataset.selection(Case("pancake", 1, "1d"), payload["grid"]).first())
        chunks = sum(handle[v].id.get_num_chunks() for v in dataset.variables)
    assert len(everything) == chunks == 12 and len(one) == 2
    assert all(length > 0 for _, length in everything)
