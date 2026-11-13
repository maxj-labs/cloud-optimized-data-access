"""The built arms, written against :class:`~benchmarks.trade_study.datasets.Dataset`.

Every arm answers a question in the same four steps, so a trial can time
them apart:

1. ``authenticate``: credentials for the source bucket.
2. ``open``: load the index. A virtual store opens its repository; a file
   reader opens the files it will read first.
3. ``locate``: find the chunks the selection needs, where the arm exposes
   that as a step of its own. An arm that cannot says why in
   ``chunk_id_reason`` and the time lands in the read.
4. ``read``: fetch and decode. Returns physical values (fill → NaN).

Settings are the existing TEMPO benchmark's; each library's authors review
their arm before a headline run (see the README).
"""

from __future__ import annotations

import asyncio
import math
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import Any

import h5py
import numpy as np
import xarray as xr
from obspec_utils.readers import BlockStoreReader

from benchmarks.tempo_no2.backends import _decode_cf, build_store, store_path
from benchmarks.tempo_no2.counters import (
    CLIENT_RANGES,
    S3fsCounter,
    fetch_object,
    probe_paging,
    trace_counts,
    traced_store,
)
from benchmarks.tempo_no2.measure import METADATA_OBJECTS, static_minimum_requests
from benchmarks.trade_study.datasets import Dataset, Selection


@dataclass
class Result:
    """times: (observation,) datetime64[ns]; values[name]: (observation, ...) float64, fill as NaN."""

    times: np.ndarray
    values: dict[str, np.ndarray]


# --- reading one granule with h5py ----------------------------------------------


def _chunk_keys(dataset: h5py.Dataset, sel: Selection) -> list[tuple[int, int]]:
    """(chunk row, chunk column) of every chunk the selection touches."""
    ch, cw = dataset.chunks[-2], dataset.chunks[-1]
    if sel.kind == "points":
        return sorted({(int(a) // ch, int(b) // cw) for a, b in zip(sel.y, sel.x, strict=True)})
    rows = range(sel.y.start // ch, (sel.y.stop - 1) // ch + 1)
    cols = range(sel.x.start // cw, (sel.x.stop - 1) // cw + 1)
    return [(r, c) for r in rows for c in cols]


def h5_locate(handle: h5py.File, variables: tuple[str, ...], sel: Selection) -> int:
    """Look up the byte range of every chunk the selection needs: HDF5's own
    chunk identification (its chunk index), with no data read. Returns the chunk count."""
    found = 0
    for path in variables:
        dataset = handle[path]
        lead = (0,) * (dataset.ndim - 2)
        ch, cw = dataset.chunks[-2], dataset.chunks[-1]
        for row, col in _chunk_keys(dataset, sel):
            dataset.id.get_chunk_info_by_coord((*lead, row * ch, col * cw))
            found += 1
    return found


def h5_select(handle: h5py.File, spec: dict[str, Any], sel: Selection) -> tuple[np.datetime64, list[np.ndarray]]:
    """The observation time and each variable's decoded values for one open granule."""
    out = []
    for path in spec["variables"]:
        dataset = handle[path]
        lead = (0,) * (dataset.ndim - 2)
        if sel.kind == "window":
            out.append(_decode_cf(dataset, dataset[(*lead, sel.y, sel.x)]))
            continue
        ys, xs = np.asarray(sel.y), np.asarray(sel.x)
        ch, cw = dataset.chunks[-2], dataset.chunks[-1]
        values = np.full(len(ys), np.nan)
        # one read per touched chunk, then pick the points inside it
        for row, col in _chunk_keys(dataset, sel):
            y_block, x_block = slice(row * ch, (row + 1) * ch), slice(col * cw, (col + 1) * cw)
            block = _decode_cf(dataset, dataset[(*lead, y_block, x_block)])
            inside = (ys // ch == row) & (xs // cw == col)
            values[inside] = block[ys[inside] - y_block.start, xs[inside] - x_block.start]
        out.append(values)
    seconds = float(np.asarray(handle[spec["time_path"]]).reshape(-1)[0])
    when = np.datetime64(spec["time_epoch"], "ns") + np.timedelta64(round(seconds * 1_000_000_000), "ns")
    return when, out


def _stack(names: list[str], per_granule: list[tuple[np.datetime64, list[np.ndarray]]]) -> Result:
    return Result(
        np.asarray([g[0] for g in per_granule], dtype="datetime64[ns]"),
        {name: np.stack([g[1][i] for g in per_granule]) for i, name in enumerate(names)},
    )


def _tuned_worker(
    store_spec: dict[str, Any], path: str, block_size: int, page_buf_size: int, paged: bool, spec: dict[str, Any], sel: Selection
) -> tuple[np.datetime64, list[np.ndarray], dict[str, Any]]:
    """One granule in a worker process (h5py holds one global lock, so threads cannot overlap reads)."""
    store, trace = traced_store(build_store(store_spec))
    kwargs = {"page_buf_size": page_buf_size} if paged else {}
    with BlockStoreReader(store, path, block_size=block_size) as reader, h5py.File(reader, "r", **kwargs) as handle:
        when, values = h5_select(handle, spec, sel)
    return when, values, trace_counts(trace)


def _ready(_: int) -> int:
    return 0


def _merge(total: dict[str, Any], counts: dict[str, Any]) -> None:
    total["request_count"] += counts["request_count"]
    total["bytes_requested"] += counts["bytes_requested"]
    for method, n in counts["requests_by_method"].items():
        total["requests_by_method"][method] = total["requests_by_method"].get(method, 0) + n


class Arm:
    name: str
    chunk_id_reason: str | None = None
    manifest_reason: str | None = None
    first_value_only = False  # set by a first-byte trial: set up for the question, read one value

    def __init__(self, dataset: Dataset, payload: dict[str, Any], settings: dict[str, Any] | None = None) -> None:
        self.dataset, self.payload = dataset, payload
        self.granules: list[dict[str, Any]] = payload["granules"]
        self.given = dict(settings or {})
        self.settings: dict[str, Any] = {}
        self.credentials: dict[str, Any] | None = None

    def authenticate(self) -> None:
        self.credentials = self.dataset.auth()

    def open(self, observations: list[int]) -> None:
        """Load the index for a question over ``observations``."""

    def locate(self, observations: list[int], sel: Selection) -> bool:
        """Identify the chunks as a step of its own; False when the arm cannot."""
        return False

    def read(self, observations: list[int], sel: Selection) -> Result:
        raise NotImplementedError

    def counts(self) -> dict[str, Any] | None:
        return None

    def close(self) -> None:
        pass


class UserStack(Arm):
    """The dataset's fsspec filesystem + ``xarray.open_mfdataset(engine="h5netcdf")``,
    default settings. Opens every granule of the question before the first value."""

    name = "user_stack"
    chunk_id_reason = "inside the read: h5netcdf resolves chunks as it reads"

    def open(self, observations: list[int]) -> None:
        ds = self.dataset
        fs = ds.filesystem(self.credentials)
        self._counter: S3fsCounter | None = None
        if hasattr(fs, "connect"):
            fs.connect()
        if hasattr(fs, "s3"):
            self._counter = S3fsCounter()
            self._counter.attach(fs)
        urls = [self.granules[o]["source_url"] for o in observations]
        kwargs: dict[str, Any] = {"engine": "h5netcdf", "combine": "nested", "concat_dim": ds.dims[0]}
        self.settings = {"engine": "h5netcdf", "group": ds.group, "defaults": True, "files_opened": len(urls)}
        data = xr.open_mfdataset([fs.open(u) for u in urls], group=ds.group, **kwargs)
        if ds.group:  # the time axis lives in the root group: a second open, as users do
            root = xr.open_mfdataset([fs.open(u) for u in urls], **kwargs)
            data = data[ds.variable_names].assign_coords({ds.dims[0]: root[ds.dims[0]]})
        self._data = data
        self._position = {o: i for i, o in enumerate(observations)}

    def read(self, observations: list[int], sel: Selection) -> Result:
        t, y, x = self.dataset.dims
        selected = self._data[self.dataset.variable_names].isel({t: [self._position[o] for o in observations]})
        selected = selected.isel(_indexers(y, x, sel)).compute()
        return _from_xarray(selected, self.dataset)

    def counts(self) -> dict[str, Any] | None:
        return self._counter.counts() if self._counter else None


def _indexers(y: str, x: str, sel: Selection) -> dict[str, Any]:
    if sel.kind == "points":
        return {y: xr.DataArray(np.asarray(sel.y), dims="point"), x: xr.DataArray(np.asarray(sel.x), dims="point")}
    return {y: sel.y, x: sel.x}


def _from_xarray(selected: xr.Dataset, dataset: Dataset) -> Result:
    times = np.asarray(selected[dataset.dims[0]].values, dtype="datetime64[ns]").reshape(-1)
    return Result(times, {name: np.asarray(selected[name].values, dtype=np.float64) for name in dataset.variable_names})


class TunedH5py(Arm):
    """h5py over an 8 MiB block reader; one process per granule when the
    question spans several. Settings follow the NSIDC cloud-optimized HDF5
    study; the page buffer applies only to paged files."""

    name = "tuned_h5py"

    def __init__(self, dataset: Dataset, payload: dict[str, Any], settings: dict[str, Any] | None = None) -> None:
        super().__init__(dataset, payload, settings)
        self.block_size = int(self.given.get("block_size", 8 * 2**20))
        self.max_workers = int(self.given.get("max_workers", 8))
        self.page_buf_size = int(self.given.get("page_buf_size", 16 * 2**20))
        self._counts: dict[str, Any] = {"request_count": 0, "bytes_requested": 0, "requests_by_method": {}, "kind": CLIENT_RANGES}
        self._pool: ProcessPoolExecutor | None = None
        self._handles: dict[int, tuple[Any, Any, Any]] = {}

    def authenticate(self) -> None:
        super().authenticate()
        self.store_spec = self.dataset.store_spec(self.credentials)

    def _path(self, observation: int) -> str:
        return store_path(self.store_spec, self.granules[observation]["source_url"])

    def _handle(self, observation: int) -> h5py.File:
        """Open (once) in this process; the trace is folded into the counts at close."""
        if observation not in self._handles:
            store, trace = traced_store(build_store(self.store_spec))
            reader = BlockStoreReader(store, self._path(observation), block_size=self.block_size)
            handle = h5py.File(reader, "r")
            if probe_paging(handle):
                handle.close()
                handle = h5py.File(reader, "r", page_buf_size=self.page_buf_size)
                self.paged = True
            self._handles[observation] = (handle, reader, trace)
        return self._handles[observation][0]

    def open(self, observations: list[int]) -> None:
        """The first granule in this process (its superblock and metadata are the
        index); worker processes for the rest, started here so client set-up is
        not charged to the read. A first-byte trial starts no workers."""
        self.paged = False
        self._handle(observations[0])
        self._pooled = len(observations) > 1 and self.max_workers > 1 and not self.first_value_only
        if self._pooled:
            # spawn, not fork: a forked child inherits obstore's async runtime and deadlocks
            self._pool = ProcessPoolExecutor(self.max_workers, mp_context=multiprocessing.get_context("spawn"))
            list(self._pool.map(_ready, range(self.max_workers)))
        self.settings = {
            "block_size": self.block_size, "max_workers": self.max_workers, "workers": "processes" if self._pooled else "in-process",
            "page_buf_size": self.page_buf_size if self.paged else 0, "paged": self.paged,
        }

    def locate(self, observations: list[int], sel: Selection) -> bool:
        if len(observations) > 1 and self._pool is not None:
            self.chunk_id_reason = "inside the read: each worker process looks up its own granule's chunks"
            return False
        for o in observations:
            h5_locate(self._handle(o), self.dataset.variables, sel)
        return True

    def read(self, observations: list[int], sel: Selection) -> Result:
        spec = self.dataset.h5_spec()
        if len(observations) > 1 and self._pool is not None:
            jobs = [(self.store_spec, self._path(o), self.block_size, self.page_buf_size, self.paged, spec, sel) for o in observations]
            results = list(self._pool.map(_tuned_worker, *zip(*jobs, strict=True)))
            for r in results:
                _merge(self._counts, r[2])
            return _stack(self.dataset.variable_names, [(r[0], r[1]) for r in results])
        return _stack(self.dataset.variable_names, [h5_select(self._handle(o), spec, sel) for o in observations])

    def counts(self) -> dict[str, Any] | None:
        total = {**self._counts, "requests_by_method": dict(self._counts["requests_by_method"])}
        for _, _, trace in self._handles.values():
            _merge(total, trace_counts(trace))
        return total

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=True)
            self._pool = None
        for handle, _, _ in self._handles.values():
            handle.close()


class Download(Arm):
    """Each granule whole into memory in concurrent range reads, then h5py on
    the in-memory image. Every byte of the file, at the network's full rate."""

    name = "download"
    manifest_reason = "no index: the whole file is fetched"
    chunk_id_reason = "no chunk selection: the whole file is fetched"

    def authenticate(self) -> None:
        super().authenticate()
        self.store_spec = self.dataset.store_spec(self.credentials)
        self.store, self._trace = traced_store(build_store(self.store_spec))
        self.part_bytes = int(self.given.get("part_bytes", 16 * 2**20))
        self.concurrency = int(self.given.get("concurrency", 16))
        self.settings = {"part_bytes": self.part_bytes, "concurrency": self.concurrency, "parse": "h5py file image"}

    def read(self, observations: list[int], sel: Selection) -> Result:
        spec, out = self.dataset.h5_spec(), []
        for o in observations:  # one granule at a time, as in the TEMPO benchmark
            path = store_path(self.store_spec, self.granules[o]["source_url"])
            data = asyncio.run(fetch_object(self.store, path, part_bytes=self.part_bytes, concurrency=self.concurrency))
            with h5py.File.in_memory(data) as handle:
                out.append(h5_select(handle, spec, sel))
        return _stack(self.dataset.variable_names, out)

    def counts(self) -> dict[str, Any] | None:
        return trace_counts(self._trace)


class VirtualIcechunk(Arm):
    """An Icechunk store of references to the original chunks, read through xarray."""

    name = "virtual_icechunk"
    chunk_id_reason = "inside the read: Icechunk fetches the chunk manifest on the first chunk request"

    def open(self, observations: list[int]) -> None:
        import zarr

        if "zarr_concurrency" in self.given:
            zarr.config.set({"async.concurrency": int(self.given["zarr_concurrency"])})
        self._data, self._time_index = self.dataset.open_virtual(self.payload, self.given)
        self.settings = {"client": "icechunk", "chunks": None, "zarr_concurrency": zarr.config.get("async.concurrency"),
                         **{k: v for k, v in self.given.items() if k.startswith("icechunk_")}}

    def read(self, observations: list[int], sel: Selection) -> Result:
        t, y, x = self.dataset.dims
        selected = self._data[self.dataset.variable_names].isel({t: self._time_index[observations]})
        return _from_xarray(selected.isel(_indexers(y, x, sel)).load(), self.dataset)

    def static_minimum(self, observations: list[int], sel: Selection) -> dict[str, Any]:
        """Requests the selection must make, from the store's chunk grid: metadata
        objects + coordinate chunks read at open + the data chunks it intersects.
        A lower bound: Icechunk may split one chunk into several ranged reads."""
        coord_chunks = 0
        for name in self.dataset.dims:
            var = self._data[name]
            if var.ndim:
                coord_chunks += math.ceil(var.shape[0] / (var.encoding.get("chunks") or var.shape)[0])
        data_chunks, nbytes = 0, 0
        steps = [int(i) for i in self._time_index[observations]]
        for name in self.dataset.variable_names:
            var = self._data[name]
            chunks = tuple(var.encoding.get("chunks") or var.shape)
            stored = np.dtype(var.encoding.get("dtype", var.dtype))  # the stored type, not the decoded one
            n, b = static_minimum_requests(tuple(var.shape), chunks, steps, sel.y, sel.x, itemsize=stored.itemsize)
            data_chunks, nbytes = data_chunks + n, nbytes + b
        return {"request_count": METADATA_OBJECTS + coord_chunks + data_chunks, "bytes_requested": nbytes, "kind": "static-minimum"}


ARM_CLASSES: dict[str, type[Arm]] = {a.name: a for a in (UserStack, TunedH5py, Download, VirtualIcechunk)}
