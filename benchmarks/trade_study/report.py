"""Medians per arm, case and measure → ``summary.json`` and ``summary.md``.

Only ``status == "ok"`` rows count. A cell is (dataset, arm, variant, case,
measure, in_region); in-region and out-of-region rows never share a cell.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

TIME_PARTS = ("auth_seconds", "manifest_seconds", "chunk_id_seconds", "byte_seconds")
MEDIANS = (*TIME_PARTS, "first_byte_seconds", "last_byte_seconds", "end_to_end_seconds", "warm_seconds", "startup_seconds",
           "request_count", "bytes_requested", "net_rx_bytes", "floor_seconds", "peak_rss_bytes",
           "requests_usd", "transfer_usd", "compute_usd", "cost_usd")
KEYS = ["dataset", "arm", "variant", "workload", "case_size", "window", "measure", "in_region"]


def load(run_dir: Path) -> pd.DataFrame:
    rows = [json.loads(line) for line in (run_dir / "results.jsonl").read_text().splitlines() if line.strip()]
    df = pd.DataFrame(rows)
    df["variant"] = df["arm_variant"].apply(lambda v: json.dumps(v, sort_keys=True) if v else "")
    return df


def summarize(df: pd.DataFrame) -> dict[str, Any]:
    ok = df[df["status"] == "ok"]
    cells = []
    for key, g in ok.groupby(KEYS, dropna=False):
        cell: dict[str, Any] = dict(zip(KEYS, key, strict=True))
        cell["case_size"], cell["in_region"] = int(cell["case_size"]), bool(cell["in_region"])
        cell["n"] = len(g)
        for name in MEDIANS:
            values = pd.to_numeric(g[name], errors="coerce").dropna() if name in g else pd.Series(dtype=float)
            cell[name] = float(values.median()) if len(values) else None
        headline = "first_byte_seconds" if cell["measure"] == "first_byte" else "end_to_end_seconds"
        seconds = pd.to_numeric(g[headline], errors="coerce")
        cell["min_s"], cell["max_s"] = float(seconds.min()), float(seconds.max())
        cell["floor_ratio"] = cell[headline] / cell["floor_seconds"] if cell["floor_seconds"] else None
        first = g.iloc[0]
        for name in ("arm_status", "section", "gate_status", "request_count_kind", "chunk_id_reason", "manifest_reason",
                     "observation_count", "index_location", "discovery_seconds", "discovery_kind", "host_kind", "client_region"):
            value = first.get(name)
            cell[name] = None if pd.isna(value) else (value.item() if hasattr(value, "item") else value)
        cells.append(cell)
    failed = df[df["status"] != "ok"]
    return {
        "run_id": df["run_id"].iloc[0], "rows": len(df), "failed": len(failed),
        "failures": failed[["arm", "workload", "case_size", "measure", "status"]].to_dict(orient="records"),
        "git_sha": df["git_sha"].dropna().iloc[0] if df["git_sha"].notna().any() else None,
        "cells": cells,
    }


def _s(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def render(summary: dict[str, Any]) -> str:
    cells = summary["cells"]
    out = [f"# Trade-study run `{summary['run_id']}`", "", f"{summary['rows']} trials, {summary['failed']} failed. Medians of cold runs, one fresh process each."]
    if cells:
        c = cells[0]
        where = "in region" if c["in_region"] else f"**out of region** (client region: {c['client_region'] or 'unknown, not AWS'}; host: {c['host_kind']})"
        context = (f"Client: {where}. Virtual-store index read from: {c['index_location']}. "
                   f"Discovery ({c['discovery_kind']}): {_s(c['discovery_seconds'])} s, measured when the granules were pinned; not in first byte.")
        out += ["", context]
    for dataset in sorted({c["dataset"] for c in cells}):
        mine = [c for c in cells if c["dataset"] == dataset]
        cases = sorted({(c["workload"], c["case_size"], c["window"]) for c in mine})
        out += ["", f"## {dataset}: time to first byte, in parts (seconds)", "",
                "| Case | Granules | Arm | n | Auth | Manifest | Chunk id | Bytes | **First byte** | min–max | Warm | Requests | MB on wire |",
                "|---|---:|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|"]
        for case in cases:
            for c in sorted((c for c in mine if (c["workload"], c["case_size"], c["window"]) == case and c["measure"] == "first_byte"), key=lambda c: c["first_byte_seconds"]):
                out.append(
                    f"| `{c['workload']}:{c['case_size']}:{c['window']}` | {c['observation_count']} | {_arm(c)} | {c['n']} | {_s(c['auth_seconds'])} | {_s(c['manifest_seconds'])} | "
                    f"{_s(c['chunk_id_seconds'])} | {_s(c['byte_seconds'])} | **{_s(c['first_byte_seconds'])}** | {_s(c['min_s'])}–{_s(c['max_s'])} | "
                    f"{_s(c['warm_seconds'], 3)} | {_count(c)} | {_mb(c['net_rx_bytes'])} |"
                )
        out += ["", f"## {dataset}: the whole question (seconds)", "",
                "| Case | Granules | Arm | n | **End to end** | min–max | Last byte | Warm | Floor | × floor | Requests | MB on wire | Peak RSS MB | $ per question |",
                "|---|---:|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for case in cases:
            for c in sorted((c for c in mine if (c["workload"], c["case_size"], c["window"]) == case and c["measure"] == "full"), key=lambda c: c["end_to_end_seconds"]):
                out.append(
                    f"| `{c['workload']}:{c['case_size']}:{c['window']}` | {c['observation_count']} | {_arm(c)} | {c['n']} | **{_s(c['end_to_end_seconds'])}** | {_s(c['min_s'])}–{_s(c['max_s'])} | "
                    f"{_s(c['last_byte_seconds'])} | {_s(c['warm_seconds'])} | {_s(c['floor_seconds'])} | {_s(c['floor_ratio'], 1)} | {_count(c)} | "
                    f"{_mb(c['net_rx_bytes'])} | {_mb(c['peak_rss_bytes'])} | {c['cost_usd']:.6f} |"
                )
    reasons = sorted({(c["arm"], c["chunk_id_reason"]) for c in cells if c["chunk_id_reason"]} | {(c["arm"], c["manifest_reason"]) for c in cells if c["manifest_reason"]})
    out += ["", "## Reading the tables", "",
            "- A dash under a part means the arm does not expose that step; its time is inside Bytes:"]
    out += [f"  - `{arm}`: {reason}." for arm, reason in reasons]
    out += [
        "- Requests marked ≥ are a static minimum from the chunk grid, not a count. Other counts are byte ranges the client asked for.",
        "- MB on wire is the host's receive counter over the trial, so it includes other traffic on a shared host.",
        "- Floor = dependent round trips × median round-trip time + bytes ÷ measured bandwidth, from the session's probe.",
        "- $ per question = S3 requests + transfer + compute at list price. Transfer is zero in region and internet egress otherwise.",
        "- Arms marked † are not gated: their values were not checked against the reference arm.",
    ]
    if summary["failures"]:
        out += ["", "## Failed trials", ""] + [f"- {f['arm']} {f['workload']}:{f['case_size']} {f['measure']}: {f['status'][:200]}" for f in summary["failures"]]
    return "\n".join(out) + "\n"


def _arm(cell: dict[str, Any]) -> str:
    name = f"`{cell['arm']}`" + (f" {cell['variant']}" if cell["variant"] else "")
    return name + ("" if cell["gate_status"] in ("pass", "reference") else " †")


def _count(cell: dict[str, Any]) -> str:
    if cell["request_count"] is None:
        return "—"
    return ("≥ " if cell["request_count_kind"] == "static-minimum" else "") + f"{cell['request_count']:,.0f}"


def _mb(value: float | None) -> str:
    return "—" if value is None else f"{value / 1e6:,.1f}"


def write_report(run_dir: Path) -> str:
    summary = summarize(load(run_dir))
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")
    text = render(summary)
    (run_dir / "summary.md").write_text(text)
    return text
