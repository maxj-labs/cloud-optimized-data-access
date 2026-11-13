"""Datasets and questions.

A dataset here is a stack of gridded granules, one observation per file,
with one or more (time, y, x) variables. Every question is asked in
coordinates (a seeded set of points, or a box of a stated side length around
the same centre) and each dataset turns it into its own grid indices, so
two datasets answer the same question on the ground.

Point-cloud and swath products (ATL03) do not fit this shape yet.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal

import numpy as np

from benchmarks.tempo_no2 import backends as tempo_backends
from benchmarks.tempo_no2 import cases as tempo_cases
from benchmarks.trade_study.scope import WORKLOAD_INFO

MANIFEST_DIR = Path(__file__).parent / "manifests"
STORE_DIR = Path(__file__).parent / "stores"  # locally built virtual stores; not committed
POINTS_OUTER_KM = 500
MAX_POINTS = 250


@dataclass(frozen=True)
class Case:
    workload: Literal["pancake", "churro", "box", "batch"]
    size: int
    window: str

    def __str__(self) -> str:
        return f"{self.workload}:{self.size}:{self.window}"


def parse_case(text: str) -> Case:
    workload, size, window = text.split(":")
    if workload not in WORKLOAD_INFO:
        raise ValueError(f"bad case {text!r}: workload must be one of {sorted(WORKLOAD_INFO)}")
    return Case(workload, int(size), window)  # type: ignore[arg-type]


def headline_cases(window: str) -> list[Case]:
    return [Case(w.name, size, window) for w in WORKLOAD_INFO.values() for size in w.sizes]  # type: ignore[arg-type]


@dataclass
class Selection:
    """Grid indices for a question: paired point indices, or a (y, x) window."""

    kind: Literal["points", "window"]
    y: Any
    x: Any

    def first(self) -> Selection:
        """The first value of the answer: one grid cell."""
        if self.kind == "points":
            return Selection("points", np.asarray(self.y)[:1], np.asarray(self.x)[:1])
        return Selection("window", slice(self.y.start, self.y.start + 1), slice(self.x.start, self.x.start + 1))


def question_points(n: int = MAX_POINTS) -> tuple[np.ndarray, np.ndarray]:
    """The seeded (lat, lon) sample every dataset is asked about: the TEMPO
    benchmark's points, drawn the same way, so the 10-point set is the first
    10 of the 250."""
    lat0, lat1, lon0, lon1 = tempo_cases.aoi_bounds(POINTS_OUTER_KM)
    rng = np.random.default_rng(tempo_cases.CONFIG.random_seed)
    lats = rng.uniform(lat0, lat1, MAX_POINTS)
    lons = rng.uniform(lon0, lon1, MAX_POINTS)
    return lats[:n], lons[:n]


class Dataset:
    """What an arm needs to know about a product. Subclasses fill in discovery,
    the grid, credentials and the virtual store."""

    name: ClassVar[str]
    region: ClassVar[str]
    variables: ClassVar[tuple[str, ...]]  # HDF5 paths
    group: ClassVar[str | None] = None  # the group xarray opens; None for the root
    time_path: ClassVar[str]
    time_epoch: ClassVar[str]
    dims: ClassVar[tuple[str, str, str]]  # (time, y, x) names in xarray
    public: ClassVar[bool] = False
    discovery_kind: ClassVar[str] = "cmr"
    index_location: ClassVar[str]  # where the virtual store's index is read from
    probe_range_mib: ClassVar[int] = 16  # bandwidth probe read size; five must fit in one granule
    windows: ClassVar[dict[str, tuple[str, str]]]

    # --- pinned inputs -----------------------------------------------------------

    def manifest_path(self, window: str) -> Path:
        return MANIFEST_DIR / f"{self.name}-{window}.json"

    def discover(self, window: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def describe_grid(self, granules: list[dict[str, Any]]) -> dict[str, Any]:
        raise NotImplementedError

    # --- questions ---------------------------------------------------------------

    def index_points(self, grid: dict[str, Any], lats: np.ndarray, lons: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        raise NotImplementedError

    def index_window(self, grid: dict[str, Any], bounds: tuple[float, float, float, float]) -> tuple[slice, slice]:
        raise NotImplementedError

    def full_window(self, grid: dict[str, Any]) -> tuple[slice, slice]:
        raise NotImplementedError

    def selection(self, case: Case, grid: dict[str, Any]) -> Selection:
        if case.workload == "churro":
            return Selection("points", *self.index_points(grid, *question_points(case.size)))
        if case.workload == "box":
            return Selection("window", *self.index_window(grid, tempo_cases.aoi_bounds(case.size)))
        return Selection("window", *self.full_window(grid))

    @staticmethod
    def observations(case: Case, n_granules: int) -> list[int]:
        """Indices into the pinned granule list that the question reads."""
        return list(range(min(case.size, n_granules) if case.workload == "pancake" else n_granules))

    # --- access ------------------------------------------------------------------

    def auth(self) -> dict[str, Any] | None:
        """Temporary credentials for the source bucket; None on public data."""
        raise NotImplementedError

    def store_spec(self, credentials: dict[str, Any] | None) -> dict[str, Any]:
        """A picklable spec for ``tempo_no2.backends.build_store``."""
        raise NotImplementedError

    def filesystem(self, credentials: dict[str, Any] | None) -> Any:
        """The fsspec filesystem a user would be handed."""
        raise NotImplementedError

    def open_virtual(self, payload: dict[str, Any], settings: dict[str, Any]) -> tuple[Any, np.ndarray]:
        """(xarray dataset over the virtual store, store time index per pinned granule)."""
        raise NotImplementedError

    def h5_spec(self) -> dict[str, Any]:
        """What a worker process needs to read a granule with h5py."""
        return {"variables": self.variables, "time_path": self.time_path, "time_epoch": self.time_epoch}

    @property
    def variable_names(self) -> list[str]:
        return [v.rsplit("/", 1)[-1] for v in self.variables]


# --- GOES-19: public, so anyone can rerun the method ------------------------------


def goes_scan_angles(grid: dict[str, Any], lats: np.ndarray, lons: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(y, x) scan angles in radians for geodetic points, on the GOES-R fixed
    grid (GOES-R Product User Guide, section 4.2.8.2). NaN where the satellite
    cannot see the point."""
    r_eq, r_pol, height = grid["semi_major_axis"], grid["semi_minor_axis"], grid["perspective_point_height"]
    phi, lam = np.deg2rad(np.asarray(lats, dtype=float)), np.deg2rad(np.asarray(lons, dtype=float) - grid["longitude_of_projection_origin"])
    e2 = 1 - (r_pol / r_eq) ** 2
    phi_c = np.arctan((r_pol / r_eq) ** 2 * np.tan(phi))
    r_c = r_pol / np.sqrt(1 - e2 * np.cos(phi_c) ** 2)
    s_x = (height + r_eq) - r_c * np.cos(phi_c) * np.cos(lam)
    s_y = -r_c * np.cos(phi_c) * np.sin(lam)
    s_z = r_c * np.sin(phi_c)
    # behind the limb the formulas still return an angle; such points are not on the grid
    visible = (height + r_eq) * ((height + r_eq) - s_x) >= s_y**2 + (r_eq / r_pol) ** 2 * s_z**2
    y, x = np.arctan(s_z / s_x), np.arcsin(-s_y / np.sqrt(s_x**2 + s_y**2 + s_z**2))
    return np.where(visible, y, np.nan), np.where(visible, x, np.nan)


class Goes19(Dataset):
    """GOES-19 ABI L2 Cloud and Moisture Imagery, full disk, band 13.

    Public in ``s3://noaa-goes19`` (us-east-1). Not in CMR, so discovery is an
    S3 listing. No public virtual store exists, so ``build-store`` writes the
    references to a local Icechunk repository: manifest retrieval for this
    dataset is a local disk read, and rows say so.
    """

    name = "goes19"
    region = "us-east-1"
    bucket = "noaa-goes19"
    product = "ABI-L2-CMIPF"
    band = "M6C13"
    variables = ("CMI",)
    time_path = "t"
    time_epoch = "2000-01-01T12:00:00"
    dims = ("t", "y", "x")
    public = True
    discovery_kind = "s3-list"
    index_location = "local-disk"
    probe_range_mib = 4  # a granule is about 24 MB
    # (year, first day of year, first hour, hours)
    windows: ClassVar[dict[str, tuple[int, int, int, int]]] = {  # type: ignore[assignment]
        "1h": (2026, 300, 15, 1),
        "1d": (2026, 300, 0, 24),
        "1w": (2026, 300, 0, 24 * 7),
        "1m": (2026, 300, 0, 24 * 30),
    }

    def _store(self) -> Any:
        return tempo_backends.build_store({"kind": "s3", "bucket": self.bucket, "region": self.region})

    def discover(self, window: str) -> list[dict[str, Any]]:
        import obstore

        year, day, hour, hours = self.windows[window]
        store = self._store()
        records = []
        for h in range(hours):
            d, hh = day + (hour + h) // 24, (hour + h) % 24
            for page in obstore.list(store, prefix=f"{self.product}/{year}/{d:03d}/{hh:02d}/"):
                for meta in page:
                    path = str(meta["path"])
                    stamp = re.search(rf"{self.band}_G19_s(\d{{4}})(\d{{3}})(\d{{2}})(\d{{2}})(\d{{2}})", path)
                    if stamp:
                        y, j, hr, mi, se = (int(g) for g in stamp.groups())
                        start = np.datetime64(f"{y}-01-01T00:00:00") + np.timedelta64(((j - 1) * 24 + hr) * 3600 + mi * 60 + se, "s")
                        records.append({
                            "granule_ur": path.rsplit("/", 1)[-1], "start_time": f"{start}Z",
                            "source_url": f"s3://{self.bucket}/{path}", "source_size_mb": meta["size"] / 1e6,
                        })
        if not records:
            raise RuntimeError(f"no {self.band} granules listed for window {window}")
        return sorted(records, key=lambda r: r["start_time"])

    def describe_grid(self, granules: list[dict[str, Any]]) -> dict[str, Any]:
        import h5py
        from obspec_utils.readers import BlockStoreReader

        path = tempo_backends.store_path(self.store_spec(None), granules[0]["source_url"])
        with BlockStoreReader(self._store(), path, block_size=2**20) as reader, h5py.File(reader, "r") as f:
            grid: dict[str, Any] = {}
            for axis in ("x", "y"):
                v = f[axis]
                grid[axis] = {"scale": float(v.attrs["scale_factor"][0]), "offset": float(v.attrs["add_offset"][0]), "size": int(v.shape[0])}
            projection = f["goes_imager_projection"].attrs
            for key in ("perspective_point_height", "semi_major_axis", "semi_minor_axis", "longitude_of_projection_origin"):
                grid[key] = float(np.asarray(projection[key]).reshape(-1)[0])
        return grid

    def index_points(self, grid: dict[str, Any], lats: np.ndarray, lons: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        y, x = goes_scan_angles(grid, lats, lons)
        out = []
        for values, axis in ((y, grid["y"]), (x, grid["x"])):
            position = (values - axis["offset"]) / axis["scale"]
            if np.isnan(position).any() or position.min() < -0.5 or position.max() > axis["size"] - 0.5:
                raise ValueError("a question point falls off the full-disk grid")
            index = np.rint(position).astype(int).clip(0, axis["size"] - 1)
            out.append(index)
        return out[0], out[1]

    def index_window(self, grid: dict[str, Any], bounds: tuple[float, float, float, float]) -> tuple[slice, slice]:
        lat0, lat1, lon0, lon1 = bounds
        ys, xs = self.index_points(grid, np.array([lat0, lat0, lat1, lat1]), np.array([lon0, lon1, lon0, lon1]))
        return slice(int(ys.min()), int(ys.max()) + 1), slice(int(xs.min()), int(xs.max()) + 1)

    def full_window(self, grid: dict[str, Any]) -> tuple[slice, slice]:
        return slice(0, grid["y"]["size"]), slice(0, grid["x"]["size"])

    def auth(self) -> dict[str, Any] | None:
        return None

    def store_spec(self, credentials: dict[str, Any] | None) -> dict[str, Any]:
        return {"kind": "s3", "bucket": self.bucket, "prefix": "", "region": self.region, "credentials": None}

    def filesystem(self, credentials: dict[str, Any] | None) -> Any:
        import s3fs

        s3fs.S3FileSystem.clear_instance_cache()
        return s3fs.S3FileSystem(anon=True)

    def store_path(self, window: str) -> Path:
        return STORE_DIR / f"{self.name}-{window}"

    def build_store(self, payload: dict[str, Any]) -> str:
        """Write virtual references for the pinned granules to a local Icechunk
        repository; returns the snapshot id. Network: reads each file's HDF5 metadata."""
        import shutil

        import icechunk
        import xarray as xr
        from obspec_utils.registry import ObjectStoreRegistry
        from virtualizarr import open_virtual_dataset
        from virtualizarr.parsers import HDFParser

        registry = ObjectStoreRegistry({f"s3://{self.bucket}": self._store()})
        parts = []
        for granule in payload["granules"]:
            vds = open_virtual_dataset(granule["source_url"], registry, HDFParser(), loadable_variables=[self.time_path])
            parts.append(vds[list(self.variables)].drop_vars([c for c in vds.coords if c not in self.dims], errors="ignore"))
        combined = xr.concat(parts, dim=self.dims[0], coords="minimal", compat="override", combine_attrs="override")
        path = self.store_path(payload["window"])
        shutil.rmtree(path, ignore_errors=True)
        config = icechunk.RepositoryConfig.default()
        config.set_virtual_chunk_container(
            icechunk.VirtualChunkContainer(f"s3://{self.bucket}/", icechunk.s3_store(region=self.region, anonymous=True))
        )
        repo = icechunk.Repository.create(icechunk.local_filesystem_storage(str(path)), config=config)
        session = repo.writable_session("main")
        combined.vz.to_icechunk(session.store)
        return session.commit(f"{len(parts)} granules, window {payload['window']}")

    def open_virtual(self, payload: dict[str, Any], settings: dict[str, Any]) -> tuple[Any, np.ndarray]:
        import icechunk
        import xarray as xr

        path = self.store_path(payload["window"])
        if not path.exists():
            raise FileNotFoundError(f"{path} missing: run `run.py build-store --dataset {self.name} --window {payload['window']}`")
        access = icechunk.containers_credentials({f"s3://{self.bucket}/": icechunk.s3_anonymous_credentials()})
        repo = icechunk.Repository.open(icechunk.local_filesystem_storage(str(path)), authorize_virtual_chunk_access=access)
        if any(k.startswith("icechunk_") for k in settings):
            repo = repo.reopen(config=tempo_backends.icechunk_config(repo, settings), authorize_virtual_chunk_access=access)
        session = repo.readonly_session(snapshot_id=payload["snapshot_id"])
        dataset = xr.open_zarr(session.store, consolidated=False, zarr_format=3, chunks=None)
        return dataset, np.arange(len(payload["granules"]))


# --- TEMPO NO2: the reference case, through the existing benchmark's access code ----


class TempoNo2(Dataset):
    """TEMPO NO₂ L3 V04 at ASDC. Needs an Earthdata login on a host in
    us-west-2; the virtual store is the public Source Cooperative mirror."""

    name = "tempo_no2"
    region = "us-west-2"
    variables = (tempo_backends.NO2_PATH, tempo_backends.QUALITY_PATH)
    group = "product"
    time_path = "time"
    time_epoch = "1980-01-06T00:00:00"
    dims = ("time", "latitude", "longitude")
    index_location = "s3 (public mirror, us-west-2)"
    windows = tempo_cases.WINDOWS

    def discover(self, window: str) -> list[dict[str, Any]]:
        return tempo_cases.discover(window).to_dict(orient="records")

    def describe_grid(self, granules: list[dict[str, Any]]) -> dict[str, Any]:
        import pandas as pd

        repo = tempo_backends.open_mirror_repo()
        snapshot_id, _ = tempo_backends.resolve_pin(repo)
        dataset = tempo_backends.VirtualIcechunk(tempo_backends.pinned_session(repo, snapshot_id).store, pd.DataFrame(granules)).dataset
        return {**tempo_cases.grid_spec(dataset["latitude"].values, dataset["longitude"].values), "snapshot_id": snapshot_id}

    def index_points(self, grid: dict[str, Any], lats: np.ndarray, lons: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        lat, lon = tempo_cases.grid_arrays(grid)
        return tempo_cases.nearest_indices(lat, lats), tempo_cases.nearest_indices(lon, lons)

    def index_window(self, grid: dict[str, Any], bounds: tuple[float, float, float, float]) -> tuple[slice, slice]:
        lat, lon = tempo_cases.grid_arrays(grid)
        return tempo_cases.coordinate_slice(lat, *bounds[:2]), tempo_cases.coordinate_slice(lon, *bounds[2:])

    def full_window(self, grid: dict[str, Any]) -> tuple[slice, slice]:
        return slice(0, int(grid["latitude"]["size"])), slice(0, int(grid["longitude"]["size"]))

    def auth(self) -> dict[str, Any] | None:
        return tempo_backends._fetch_asdc_credentials()

    def store_spec(self, credentials: dict[str, Any] | None) -> dict[str, Any]:
        return tempo_backends.source_store_spec(credentials)

    def filesystem(self, credentials: dict[str, Any] | None) -> Any:
        return tempo_backends._earthaccess_filesystem()

    def open_repo(self, settings: dict[str, Any]) -> Any:
        return tempo_backends.open_mirror_repo("s3", {k: v for k, v in settings.items() if k.startswith("icechunk_")})

    def open_virtual(self, payload: dict[str, Any], settings: dict[str, Any]) -> tuple[Any, np.ndarray]:
        import pandas as pd
        import xarray as xr

        session = tempo_backends.pinned_session(self.open_repo(settings), payload["snapshot_id"])
        dataset = xr.open_zarr(session.store, consolidated=False, chunks=None)
        matcher = tempo_backends._VirtualSelection(dataset, pd.DataFrame(payload["granules"]))
        matcher.assert_one_to_one()
        return dataset, matcher.time_indices


DATASETS: dict[str, Dataset] = {d.name: d for d in (Goes19(), TempoNo2())}


def timed_discovery(dataset: Dataset, window: str, reps: int = 3) -> tuple[list[dict[str, Any]], float]:
    """(granules, median seconds over ``reps`` searches). Network."""
    seconds, granules = [], []
    for _ in range(reps):
        start = time.perf_counter()
        granules = dataset.discover(window)
        seconds.append(time.perf_counter() - start)
    return granules, float(np.median(seconds))
