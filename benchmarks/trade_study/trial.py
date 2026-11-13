"""One trial: a question, one arm, timed in the parts the scope names.

Two kinds of trial, each a fresh process:

- ``first_byte``: the arm sets up for the whole question, then reads only
  the first value of the answer. ``first_byte_seconds`` is the sum of
  authentication, manifest retrieval, chunk identification and that read.
- ``full``: the whole question. ``last_byte_seconds`` is when every value is
  in memory; ``end_to_end_seconds`` adds the statistic computed from them.

Discovery is timed when the granule list is pinned and copied into the row
for context; it is never part of first byte.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field, fields
from typing import Any

import numpy as np

from benchmarks.tempo_no2.measure import (
    METADATA_OBJECTS,
    S3_GET_USD_PER_1000,
    _json_default,
    cpu_steal_seconds,
    floor_seconds,
    resource_usage,
)
from benchmarks.trade_study.arms import Arm, Result
from benchmarks.trade_study.datasets import Case, Dataset
from benchmarks.trade_study.scope import ARM_INFO

EGRESS_USD_PER_GB = 0.09  # AWS internet egress, first 10 TB tier, list price 2026-11-02; paid by the bucket owner
MEASURES = ("first_byte", "full")


@dataclass
class Row:
    # identity
    run_id: str | None = None
    session_id: str | None = None
    dataset: str | None = None
    arm: str | None = None
    arm_status: str | None = None
    section: int | None = None
    arm_settings: dict[str, Any] = field(default_factory=dict)
    arm_variant: dict[str, Any] = field(default_factory=dict)
    workload: str | None = None
    case_size: int | None = None
    window: str | None = None
    measure: str | None = None
    observation_count: int | None = None
    repetition: int | None = None
    cache_state: str = "cold"
    transport: str = "s3"
    client_region: str | None = None
    in_region: bool | None = None
    # provenance
    git_sha: str | None = None
    uv_lock_sha256: str | None = None
    packages: dict[str, str] = field(default_factory=dict)
    manifest_path: str | None = None
    snapshot_id: str | None = None
    index_location: str | None = None
    host_kind: str | None = None
    instance_type: str | None = None
    availability_zone: str | None = None
    cpu_model: str | None = None
    nproc: int | None = None
    # time, in the scope's parts
    discovery_seconds: float | None = None
    discovery_kind: str | None = None
    startup_seconds: float | None = None  # process start and imports; outside every other number
    auth_seconds: float | None = None
    auth_reason: str | None = None
    manifest_seconds: float | None = None
    manifest_reason: str | None = None
    chunk_id_seconds: float | None = None
    chunk_id_reason: str | None = None
    byte_seconds: float | None = None
    first_byte_seconds: float | None = None
    last_byte_seconds: float | None = None
    processing_seconds: float | None = None
    end_to_end_seconds: float | None = None
    warm_seconds: float | None = None  # the same read again, same process
    # resources
    cpu_user_s: float | None = None
    cpu_sys_s: float | None = None
    peak_rss_bytes: int | None = None
    load_avg_1m: float | None = None
    cpu_steal_s: float | None = None
    # transport
    request_count: int | None = None
    request_count_kind: str | None = None
    requests_by_method: dict[str, int] = field(default_factory=dict)
    bytes_requested: int | None = None
    net_rx_bytes: int | None = None
    # floor and cost
    rtt_ms_median: float | None = None
    bandwidth_mb_s: float | None = None
    floor_seconds: float | None = None
    requests_usd: float | None = None
    transfer_usd: float | None = None
    compute_usd: float | None = None
    cost_usd: float | None = None
    # outcome
    answer_checksum: float | None = None
    gate_status: str | None = None
    status: str | None = None
    error: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=_json_default)

    @classmethod
    def from_json(cls, text: str) -> Row:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in json.loads(text).items() if k in known})


def answer(result: Result) -> dict[str, np.ndarray]:
    """The statistic the analyst wanted: each variable's mean per observation, NaN-aware."""
    out = {}
    for name, values in result.values.items():
        flat = values.reshape(values.shape[0], -1)
        valid = ~np.isnan(flat)
        with np.errstate(invalid="ignore"):
            out[name] = np.where(valid.any(axis=1), np.nansum(flat, axis=1) / np.maximum(valid.sum(axis=1), 1), np.nan)
    return out


def serial_round_trips(arm: str, request_count: int, n_obs: int) -> int:
    """Dependent round trips on the critical path, for the floor. Virtual: the
    metadata objects, then one round of concurrent chunk reads. Download: per
    granule, a HEAD and one round of ranges. Other file readers: granules are
    read concurrently, so one granule's mean request chain."""
    if arm.startswith("virtual"):
        return METADATA_OBJECTS + 1
    if arm == "download":
        return 2 * n_obs
    return max(1, round(request_count / max(1, n_obs)))


def price(row: Row, instance_rate_usd_h: float) -> None:
    """Requests, transfer and compute at list price. Transfer is zero in region;
    out of region it is internet egress on the bytes the wire carried."""
    seconds = row.end_to_end_seconds if row.measure == "full" else row.first_byte_seconds
    wire = row.net_rx_bytes if row.net_rx_bytes is not None else (row.bytes_requested or 0)
    row.requests_usd = (row.request_count or 0) / 1000 * S3_GET_USD_PER_1000
    row.transfer_usd = 0.0 if row.in_region else wire / 1e9 * EGRESS_USD_PER_GB
    row.compute_usd = (seconds or 0.0) / 3600 * instance_rate_usd_h
    row.cost_usd = row.requests_usd + row.transfer_usd + row.compute_usd


def _net_rx() -> int:
    import psutil

    return psutil.net_io_counters().bytes_recv


def _live_children_cpu() -> tuple[float, float]:
    import psutil

    user = system = 0.0
    for child in psutil.Process().children(recursive=True):
        try:
            times = child.cpu_times()
        except psutil.Error:
            continue
        user, system = user + times.user, system + times.system
    return user, system


def run_trial(
    arm: Arm,
    dataset: Dataset,
    case: Case,
    measure: str,
    *,
    rep: int = 0,
    probes: dict[str, Any] | None = None,
    instance_rate_usd_h: float = 0.192,
    warm: bool = True,
    in_region: bool = False,
) -> Row:
    info = ARM_INFO[arm.name]
    payload = arm.payload
    observations = dataset.observations(case, len(payload["granules"]))
    selection = dataset.selection(case, payload["grid"])
    read_obs, read_sel = (observations[:1], selection.first()) if measure == "first_byte" else (observations, selection)
    row = Row(
        dataset=dataset.name, arm=arm.name, arm_status=info.status, section=info.section, arm_variant=dict(arm.given),
        workload=case.workload, case_size=case.size, window=case.window, measure=measure,
        observation_count=len(observations), repetition=rep, in_region=in_region,
        discovery_seconds=payload.get("discovery_seconds"), discovery_kind=dataset.discovery_kind,
        snapshot_id=payload.get("snapshot_id"), index_location=payload.get("index_location"),
    )
    load_before, steal_before, net_before = os.getloadavg()[0], cpu_steal_seconds(), _net_rx()
    clock = time.perf_counter
    start = clock()
    try:
        arm.first_value_only = measure == "first_byte"
        with resource_usage() as usage:
            arm.authenticate()
            t_auth = clock()
            arm.open(observations)
            t_open = clock()
            located = arm.locate(read_obs, read_sel)
            t_located = clock()
            result = arm.read(read_obs, read_sel)
            t_read = clock()
            stats = answer(result)
            t_done = clock()
            kids_user, kids_sys = _live_children_cpu()  # worker processes still alive for the warm query
        row.auth_seconds = t_auth - start
        row.auth_reason = "public bucket: no credentials" if dataset.public else None
        row.manifest_seconds, row.manifest_reason = t_open - t_auth, arm.manifest_reason
        if located:
            row.chunk_id_seconds, row.byte_seconds = t_located - t_open, t_read - t_located
        else:
            row.chunk_id_reason, row.byte_seconds = arm.chunk_id_reason, t_read - t_open
        if measure == "first_byte":
            row.first_byte_seconds = t_read - start
        else:
            row.last_byte_seconds = t_read - start
            row.processing_seconds, row.end_to_end_seconds = t_done - t_read, t_done - start
        row.arm_settings = dict(arm.settings)
        row.cpu_user_s, row.cpu_sys_s, row.peak_rss_bytes = usage.cpu_user_s + kids_user, usage.cpu_sys_s + kids_sys, usage.peak_rss_bytes
        row.net_rx_bytes = _net_rx() - net_before
        row.answer_checksum = float(np.nansum([np.nansum(v) for v in stats.values()]))
        counts = arm.counts()
        if counts is None and hasattr(arm, "static_minimum"):
            counts = arm.static_minimum(read_obs, read_sel)
        if counts is not None:
            row.request_count, row.bytes_requested = counts["request_count"], counts["bytes_requested"]
            row.request_count_kind = counts.get("kind")
            row.requests_by_method = counts.get("requests_by_method", {})
        if probes and probes.get("rtt_ms_median") and probes.get("bandwidth_mb_s"):
            row.rtt_ms_median, row.bandwidth_mb_s = probes["rtt_ms_median"], probes["bandwidth_mb_s"]
            # a static bound counts uncompressed chunk bytes; the floor wants bytes on the wire
            floor_bytes = row.net_rx_bytes if row.request_count_kind == "static-minimum" else (row.bytes_requested or 0)
            row.floor_seconds = floor_seconds(
                serial_round_trips(arm.name, row.request_count or 0, len(read_obs)),
                probes["rtt_ms_median"] / 1000, floor_bytes, probes["bandwidth_mb_s"] * 1e6,
            )
        price(row, instance_rate_usd_h)
        if warm:
            warm_start = clock()
            arm.read(read_obs, read_sel)
            row.warm_seconds = clock() - warm_start
        row.status = "ok"
    except Exception as error:  # noqa: BLE001 -- a failed trial is a row with its error, not a crashed session
        row.status = f"{type(error).__name__}: {error}"[:2000]
        row.error = row.status
    finally:
        try:
            arm.close()
        except Exception:  # noqa: BLE001, S110 -- teardown is outside the measurement
            pass
    row.load_avg_1m = (load_before + os.getloadavg()[0]) / 2
    steal_after = cpu_steal_seconds()
    if steal_before is not None and steal_after is not None:
        row.cpu_steal_s = steal_after - steal_before
    return row
