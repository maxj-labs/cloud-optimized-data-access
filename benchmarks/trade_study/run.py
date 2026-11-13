"""CLI: status | discover | build-store | probe | gate | trial | session | report | superchunks.

``trial`` runs one trial in this process and prints one JSON row as the last
line of stdout; ``session`` spawns one fresh process per trial, in shuffled
order. The GOES-19 dataset needs no credentials, so every step can be rerun
by anyone.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from benchmarks.tempo_no2.measure import host_info
from benchmarks.tempo_no2.run import provenance
from benchmarks.trade_study import superchunks as sc
from benchmarks.trade_study.arms import ARM_CLASSES, Arm, Result
from benchmarks.trade_study.datasets import (
    DATASETS,
    Case,
    Dataset,
    headline_cases,
    parse_case,
    timed_discovery,
)
from benchmarks.trade_study.scope import ARM_INFO, render_status
from benchmarks.trade_study.trial import MEASURES, Row, run_trial

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS = Path("results/trade_study")
GATE_RTOL = 1e-6  # h5py decodes packed integers in float64, xarray in float32
DEFAULT_ARMS = ("user_stack", "tuned_h5py", "download", "virtual_icechunk")
DEFAULT_INSTANCE_RATE = float(os.environ.get("BENCH_INSTANCE_RATE_USD_H", "0.192"))  # m6i.xlarge, 2026-11-02


def load_manifest(dataset: Dataset, window: str) -> dict[str, Any]:
    path = dataset.manifest_path(window)
    if not path.exists():
        raise SystemExit(f"{path} missing: run `run.py discover --dataset {dataset.name} --window {window}` first")
    return json.loads(path.read_text())


def make_arm(name: str, dataset: Dataset, payload: dict[str, Any], settings: dict[str, Any] | None = None) -> Arm:
    if name not in ARM_CLASSES:
        info = ARM_INFO.get(name)
        if info is None:
            raise SystemExit(f"unknown arm {name!r}; one of {sorted(ARM_INFO)}")
        raise SystemExit(f"arm {name!r} is {info.status}, not built. Needs: {info.needs or 'see the README'}")
    return ARM_CLASSES[name](dataset, payload, settings)


def client_region(host: dict[str, Any]) -> str | None:
    """The AWS region this client runs in: BENCH_CLIENT_REGION, else the EC2 zone, else unknown."""
    zone = host.get("availability_zone")
    return os.environ.get("BENCH_CLIENT_REGION") or (zone[:-1] if zone else None)


def parse_arms(value: str) -> tuple[str, ...]:
    return tuple(a.strip() for a in value.split(",") if a.strip())


# --- gate -------------------------------------------------------------------------


def compare(reference: Result, other: Result) -> tuple[str, float]:
    """("exact" | "close" | "vacuous", largest relative difference). Raises on a mismatch."""
    offsets = np.abs((reference.times - other.times) / np.timedelta64(1, "us")) if reference.times.shape == other.times.shape else np.array([np.inf])
    if offsets.size and offsets.max() > 1:
        raise AssertionError("observation times differ")
    worst, vacuous = 0.0, True
    for name, expected in reference.values.items():
        got = other.values[name]
        if expected.shape != got.shape:
            raise AssertionError(f"{name}: shape {got.shape}, expected {expected.shape}")
        if not np.array_equal(np.isnan(expected), np.isnan(got)):
            raise AssertionError(f"{name}: fill masks differ")
        valid = ~np.isnan(expected)
        if valid.any():
            vacuous = False
            scale = np.maximum(np.abs(expected[valid]), np.finfo(float).tiny)
            worst = max(worst, float((np.abs(got[valid] - expected[valid]) / scale).max()))
    if worst > GATE_RTOL:
        raise AssertionError(f"values differ: largest relative difference {worst:.3g} > {GATE_RTOL}")
    return ("vacuous" if vacuous else "exact" if worst == 0 else "close"), worst


def gate_observations(n: int) -> list[int]:
    return sorted({0, n // 2, n - 1})


def run_gate(dataset: Dataset, payload: dict[str, Any], arm_names: tuple[str, ...], cases: list[Case]) -> list[dict[str, Any]]:
    """Every arm against the first one, on the first, middle and last observation of each case."""
    arms = []
    for name in arm_names:
        arm = make_arm(name, dataset, payload)
        arm.authenticate()
        arms.append(arm)
    reference, rows = arms[0], []
    try:
        for case in cases:
            selection = dataset.selection(case, payload["grid"])
            for observation in gate_observations(len(dataset.observations(case, len(payload["granules"])))):
                results = []
                for arm in arms:
                    try:
                        arm.open([observation])
                        results.append(arm.read([observation], selection))
                    except Exception as error:  # noqa: BLE001 -- a read failure is a gate row, not a crash
                        results.append(error)
                for arm, result in zip(arms[1:], results[1:], strict=True):
                    try:
                        for r in (results[0], result):
                            if isinstance(r, Exception):
                                raise r
                        status, worst = compare(results[0], result)
                    except Exception as error:  # noqa: BLE001
                        status, worst = f"fail: {type(error).__name__}: {str(error)[:300]}", float("nan")
                    rows.append({"case": str(case), "arm": arm.name, "reference": reference.name, "observation": observation,
                                 "status": status, "max_rel_diff": worst})
                    print(f"gate {case} {arm.name} vs {reference.name} observation {observation}: {status}", file=sys.stderr, flush=True)
    finally:
        for arm in arms:
            arm.close()
    return rows


def gate_verdicts(rows: list[dict[str, Any]], reference: str) -> dict[str, str]:
    """Per arm: "pass" when no comparison failed and at least one per case was not vacuous."""
    verdicts = {reference: "reference"}
    for arm in sorted({r["arm"] for r in rows}):
        mine = [r for r in rows if r["arm"] == arm]
        failed = any(r["status"].startswith("fail") for r in mine)
        proven = all(any(r["status"] in ("exact", "close") for r in mine if r["case"] == case) for case in {r["case"] for r in mine})
        verdicts[arm] = "fail" if failed else "pass" if proven else "vacuous"
    return verdicts


def gate_path(dataset: Dataset, window: str) -> Path:
    return RESULTS / "gates" / f"{dataset.name}-{window}.json"


# --- subcommands ------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    print(render_status())
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    dataset = DATASETS[args.dataset]
    granules, seconds = timed_discovery(dataset, args.window)
    grid = dataset.describe_grid(granules)
    payload = {
        "created_at": datetime.now(UTC).isoformat(), "dataset": dataset.name, "window": args.window,
        "discovery_kind": dataset.discovery_kind, "discovery_seconds": seconds,
        "environment": {"python": sys.version.split()[0], **provenance()},
        "snapshot_id": grid.pop("snapshot_id", None), "index_location": dataset.index_location,
        "grid": grid, "granules": granules,
    }
    path = dataset.manifest_path(args.window)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    print(f"wrote {path}: {len(granules)} granules, discovery ({dataset.discovery_kind}) {seconds:.2f} s")
    return 0


def cmd_build_store(args: argparse.Namespace) -> int:
    dataset = DATASETS[args.dataset]
    if not hasattr(dataset, "build_store"):
        raise SystemExit(f"{dataset.name} reads an existing virtual store; nothing to build")
    payload = load_manifest(dataset, args.window)
    start = time.perf_counter()
    payload["snapshot_id"] = dataset.build_store(payload)
    payload["store_build_seconds"] = time.perf_counter() - start
    dataset.manifest_path(args.window).write_text(json.dumps(payload, indent=2, default=str) + "\n")
    print(f"built {dataset.store_path(args.window)} in {payload['store_build_seconds']:.0f} s; snapshot {payload['snapshot_id']} pinned in the manifest")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    from benchmarks.tempo_no2.backends import build_store, store_path
    from benchmarks.tempo_no2.counters import probe_download, probe_network

    dataset = DATASETS[args.dataset]
    payload = load_manifest(dataset, args.window)
    spec = dataset.store_spec(dataset.auth())
    network = probe_network(build_store(spec), store_path(spec, payload["granules"][0]["source_url"]), range_reads=args.range_reads, range_bytes=(args.range_mib or dataset.probe_range_mib) * 2**20)
    # A floor needs the fastest rate the link has shown, and arms read with many streams:
    # also fetch the granule whole with 16 ranges in flight, and keep the best rate seen.
    path = store_path(spec, payload["granules"][0]["source_url"])
    concurrent = max(probe_download(build_store(spec), path, part_bytes=2 * 2**20, concurrency=16)["mb_s"] for _ in range(3))
    network = {**network, "bandwidth_single_stream_mb_s": network["bandwidth_mb_s"], "bandwidth_concurrent_mb_s": concurrent,
               "bandwidth_mb_s": max(network["bandwidth_mb_s"], concurrent)}
    host = host_info()
    region = client_region(host)
    out = {"created_at": datetime.now(UTC).isoformat(), "dataset": dataset.name, **network, "host": host,
           "client_region": region, "data_region": dataset.region, "in_region": region == dataset.region}
    path = Path(args.out or RESULTS / f"probes-{dataset.name}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out, indent=2))
    return 0


def cmd_gate(args: argparse.Namespace) -> int:
    dataset = DATASETS[args.dataset]
    payload = load_manifest(dataset, args.window)
    # a batch reads what a pancake reads, over more observations; the pancake gate covers it
    cases = args.case or [c for c in headline_cases(args.window) if c.workload != "batch" and not (c.workload == "pancake" and c.size != 1)]
    rows = run_gate(dataset, payload, args.arms, cases)
    verdicts = gate_verdicts(rows, args.arms[0])
    path = gate_path(dataset, args.window)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"created_at": datetime.now(UTC).isoformat(), "rtol": GATE_RTOL, "verdicts": verdicts, "rows": rows}, indent=2) + "\n")
    for arm, verdict in verdicts.items():
        print(f"{arm}: {verdict}")
    ok = all(v in ("pass", "reference") for v in verdicts.values())
    print("GATE PASSED" if ok else "GATE FAILED")
    return 0 if ok else 1


def stamp(row: Row, dataset: Dataset, window: str, run_id: str, session: str) -> None:
    prov, host = provenance(), host_info()
    row.run_id, row.session_id = run_id, session
    row.git_sha, row.uv_lock_sha256, row.packages = prov["git_sha"], prov["uv_lock_sha256"], prov["packages"]
    try:
        row.manifest_path = str(dataset.manifest_path(window).resolve().relative_to(REPO_ROOT))
    except ValueError:
        row.manifest_path = str(dataset.manifest_path(window))
    row.host_kind, row.instance_type, row.availability_zone = host["host_kind"], host["instance_type"], host["availability_zone"]
    row.cpu_model, row.nproc = host["cpu_model"], host["nproc"]
    row.client_region = client_region(host)
    gates = gate_path(dataset, window)
    if gates.exists():
        row.gate_status = json.loads(gates.read_text())["verdicts"].get(row.arm or "", "not gated")
    else:
        row.gate_status = "not gated"


def cmd_trial(args: argparse.Namespace) -> int:
    import psutil

    startup = time.time() - psutil.Process().create_time()
    dataset = DATASETS[args.dataset]
    payload = load_manifest(dataset, args.case.window)
    probes = json.loads(Path(args.probes).read_text()) if args.probes else None
    settings = json.loads(args.settings) if args.settings else None
    region = client_region(host_info())
    row = run_trial(make_arm(args.arm, dataset, payload, settings), dataset, args.case, args.measure, rep=args.rep, probes=probes,
                    instance_rate_usd_h=args.instance_rate_usd_h, warm=not args.no_warm, in_region=region == dataset.region)
    row.startup_seconds = startup
    stamp(row, dataset, args.case.window, args.run_id, args.session)
    print(row.to_json())
    return 0 if row.status == "ok" else 1


def plan(arms: tuple[str, ...], cases: list[Case], reps: int, variants: list[tuple[str, dict[str, Any]]], seed: int = 20250314) -> list[dict[str, Any]]:
    """Every (arm, case, measure) once per repetition, order shuffled inside each repetition."""
    rng = random.Random(seed)
    specs = []
    for rep in range(reps):
        block = [{"arm": arm, "case": case, "measure": measure, "rep": rep, "settings": {}} for arm in arms for case in cases for measure in MEASURES]
        block += [{"arm": arm, "case": case, "measure": "full", "rep": rep, "settings": settings} for arm, settings in variants if arm in arms for case in cases if case.workload == "box"]
        rng.shuffle(block)
        specs += block
    return specs


def cmd_session(args: argparse.Namespace) -> int:
    dataset = DATASETS[args.dataset]
    payload = load_manifest(dataset, args.window)
    for arm in args.arms:
        make_arm(arm, dataset, payload)  # refuses arms that are not built
    cases = args.case or headline_cases(args.window)
    variants = [(arm, json.loads(settings)) for arm, settings in (v.split("=", 1) for v in args.variants)]
    specs = plan(args.arms, cases, args.reps, variants)
    print(f"{len(specs)} trials: {len(args.arms)} arms × {len(cases)} cases × {len(MEASURES)} measures × {args.reps} repetitions"
          f"{' + variants' if variants else ''}, each a fresh process, over {len(payload['granules'])} pinned granules")
    gates = gate_path(dataset, args.window)
    verdicts = json.loads(gates.read_text())["verdicts"] if gates.exists() else {}
    ungated = [a for a in args.arms if verdicts.get(a) not in ("pass", "reference")]
    if ungated and not args.allow_ungated:
        print(f"not gated: {ungated}. Run `run.py gate --dataset {dataset.name} --window {args.window}` first, or pass --allow-ungated.", file=sys.stderr)
        return 2
    if not args.yes:
        print("re-run with --yes to start")
        return 2
    probes = args.probes or str(RESULTS / f"probes-{dataset.name}.json")
    if not Path(probes).exists():
        print(f"warning: no probes at {probes}; floor_seconds will be null (run `run.py probe` first)", file=sys.stderr)
        probes = None
    out_dir = RESULTS / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    failed = 0
    with (out_dir / "results.jsonl").open("a") as results:
        for index, spec in enumerate(specs):
            cmd = [sys.executable, "-m", "benchmarks.trade_study.run", "trial", "--dataset", dataset.name, "--arm", spec["arm"],
                   "--case", str(spec["case"]), "--measure", spec["measure"], "--rep", str(spec["rep"]), "--run-id", args.run_id,
                   "--session", args.session, "--instance-rate-usd-h", str(args.instance_rate_usd_h)]
            if spec["settings"]:
                cmd += ["--settings", json.dumps(spec["settings"])]
            if probes:
                cmd += ["--probes", probes]
            row = None
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout_s, check=False)
                last = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
                try:
                    row = Row.from_json(last)
                except Exception:  # noqa: BLE001 -- not a row
                    reason, tail = f"exit {proc.returncode}, unparseable output", proc.stderr[-2000:]
            except subprocess.TimeoutExpired:
                reason, tail = f"timeout after {args.timeout_s:.0f} s", ""
            if row is None:
                row = Row(run_id=args.run_id, session_id=args.session, dataset=dataset.name, arm=spec["arm"], arm_variant=spec["settings"],
                          workload=spec["case"].workload, case_size=spec["case"].size, window=spec["case"].window, measure=spec["measure"],
                          repetition=spec["rep"], status=f"subprocess: {reason}", error=tail)
            failed += row.status != "ok"
            results.write(row.to_json() + "\n")
            results.flush()
            seconds = row.first_byte_seconds if spec["measure"] == "first_byte" else row.end_to_end_seconds
            print(f"[{index + 1}/{len(specs)}] {spec['arm']} {spec['case']} {spec['measure']} rep {spec['rep']}: "
                  f"{row.status if row.status != 'ok' else f'{seconds:.2f} s'}", file=sys.stderr, flush=True)
    print(f"done: {len(specs)} trials, {failed} failed, rows in {out_dir / 'results.jsonl'}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from benchmarks.trade_study.report import write_report

    print(write_report(Path(args.run_dir)))
    return 0


def cmd_superchunks(args: argparse.Namespace) -> int:
    import h5py
    from obspec_utils.readers import BlockStoreReader

    from benchmarks.tempo_no2.backends import build_store, store_path

    dataset = DATASETS[args.dataset]
    payload = load_manifest(dataset, args.window)
    spec = dataset.store_spec(dataset.auth())
    probes_path = RESULTS / f"probes-{dataset.name}.json"
    probes = json.loads(probes_path.read_text()) if probes_path.exists() else None
    n = len(payload["granules"])
    path = store_path(spec, payload["granules"][0]["source_url"])
    rows = []
    with BlockStoreReader(build_store(spec), path, block_size=2**20) as reader, h5py.File(reader, "r") as handle:
        for case in headline_cases(args.window):
            n_obs = len(dataset.observations(case, n))
            ranges = sc.needed_ranges(handle, dataset.variables, dataset.selection(case, payload["grid"]))
            for g in sc.analyse(ranges, n_obs, max_bytes=args.max_mib * 2**20):
                rows.append({
                    "case": str(case), "granules": n_obs, "max_gap_bytes": g.max_gap, "requests": g.requests,
                    "bytes_read": g.bytes_read, "bytes_needed": g.bytes_needed, "over_read": g.over_read,
                    "floor_seconds": sc.floor(g, probes["rtt_ms_median"] / 1000, probes["bandwidth_mb_s"] * 1e6) if probes else None,
                })
    out = {"created_at": datetime.now(UTC).isoformat(), "dataset": dataset.name, "window": args.window, "max_bytes": args.max_mib * 2**20,
           "layout_from": payload["granules"][0]["granule_ur"], "assumption": "every granule has the first granule's chunk layout",
           "probes": {k: probes[k] for k in ("rtt_ms_median", "bandwidth_mb_s")} if probes else None, "rows": rows}
    target = RESULTS / f"superchunks-{dataset.name}-{args.window}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(out, indent=2) + "\n")
    print("| Case | Granules | Grouping | Requests | MB read | Over-read | Floor s |\n|---|---:|---|---:|---:|---:|---:|")
    for r in rows:
        label = "one request per chunk" if r["max_gap_bytes"] < 0 else f"gap ≤ {r['max_gap_bytes'] // 1024:,} KiB"
        floor = f"{r['floor_seconds']:.2f}" if r["floor_seconds"] is not None else "—"
        print(f"| `{r['case']}` | {r['granules']} | {label} | {r['requests']:,} | {r['bytes_read'] / 1e6:.2f} | {r['over_read']:.0%} | {floor} |")
    print(f"\nwrote {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m benchmarks.trade_study.run", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, fn: Any, help_: str, dataset: bool = True, window: bool = True) -> argparse.ArgumentParser:
        s = sub.add_parser(name, help=help_)
        s.set_defaults(fn=fn)
        if dataset:
            s.add_argument("--dataset", choices=sorted(DATASETS), default="goes19")
        if dataset and window:
            s.add_argument("--window", default="1h", help="1h (GOES-19 only), 1d, 1w or 1m")
        return s

    add("status", cmd_status, "the scope tables with each row's real status", dataset=False)
    add("discover", cmd_discover, "find and pin the window's granules; times the search (network)")
    add("build-store", cmd_build_store, "write virtual references for the pinned granules to a local Icechunk store (network)")
    s = add("probe", cmd_probe, "round-trip time and bandwidth to the source bucket (network)")
    s.add_argument("--out")
    s.add_argument("--range-reads", type=int, default=5)
    s.add_argument("--range-mib", type=int, help="size of each bandwidth read; five must fit in one granule (default per dataset)")
    s = add("gate", cmd_gate, "every arm must return the first arm's values (network)")
    s.add_argument("--arms", type=parse_arms, default=("tuned_h5py", "user_stack", "download", "virtual_icechunk"), help="comma-separated; the first is the reference")
    s.add_argument("--case", type=parse_case, action="append")
    s = add("trial", cmd_trial, "one trial in this process; prints one JSON row (network)", window=False)
    s.add_argument("--arm", required=True)
    s.add_argument("--case", type=parse_case, required=True, help="workload:size:window, e.g. churro:10:1h")
    s.add_argument("--measure", choices=MEASURES, default="full")
    s.add_argument("--rep", type=int, default=0)
    s.add_argument("--run-id", default="adhoc")
    s.add_argument("--session", default="1")
    s.add_argument("--probes")
    s.add_argument("--settings", help='JSON of arm settings, e.g. \'{"max_workers": 1}\'')
    s.add_argument("--no-warm", action="store_true", help="skip the warm second query")
    s.add_argument("--instance-rate-usd-h", type=float, default=DEFAULT_INSTANCE_RATE)
    s = add("session", cmd_session, "the matrix, one fresh process per trial, shuffled (network)")
    s.add_argument("--run-id", required=True)
    s.add_argument("--session", default="1")
    s.add_argument("--arms", type=parse_arms, default=DEFAULT_ARMS)
    s.add_argument("--case", type=parse_case, action="append", help="repeatable; default: the headline cases for the window")
    s.add_argument("--reps", type=int, default=10)
    s.add_argument("--variants", action="append", default=[], help='arm=JSON, run on the box case, e.g. \'tuned_h5py={"max_workers": 1}\'')
    s.add_argument("--probes")
    s.add_argument("--timeout-s", type=float, default=1800)
    s.add_argument("--instance-rate-usd-h", type=float, default=DEFAULT_INSTANCE_RATE)
    s.add_argument("--allow-ungated", action="store_true")
    s.add_argument("--yes", action="store_true", help="start after printing the plan")
    s = add("report", cmd_report, "medians per arm and case → summary.json and summary.md", dataset=False)
    s.add_argument("run_dir")
    s = add("superchunks", cmd_superchunks, "requests and bytes if chunks were grouped into larger reads (network: one granule's chunk index)")
    s.add_argument("--max-mib", type=int, default=64, help="largest grouped read")
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
