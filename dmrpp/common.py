"""What the survey, the listing and the verifier share, so their outputs can be compared."""

from __future__ import annotations

import time
from typing import Any

CMR = "https://cmr.earthdata.nasa.gov/search/granules.umm_json"
# Matched against the lower-cased name, so ``.HDF5`` and ``.NC4`` count too.
DATA_SUFFIXES = (".nc", ".nc4", ".nc3", ".h5", ".hdf", ".hdf5", ".he5", ".h4", ".he4")
SIDECAR_SUFFIX = ".dmrpp"
DAY = 86400
# (label, at least, less than) in seconds: how much newer a data file is than its sidecar
GAPS = (
    ("under 1 hour", 0, 3600),
    ("1 hour to 1 day", 3600, DAY),
    ("1 to 30 days", DAY, 30 * DAY),
    ("30 days to 1 year", 30 * DAY, 365 * DAY),
    ("over 1 year", 365 * DAY, float("inf")),
)
CREDENTIALS_RENEW_AFTER_S = 45 * 60  # a DAAC's temporary S3 credentials last an hour


def is_data_file(name: str) -> bool:
    """True for a name with an HDF or netCDF suffix, in any case."""
    return name.lower().endswith(DATA_SUFFIXES)


def gap_label(seconds: float) -> str:
    """The :data:`GAPS` bucket a gap falls in."""
    return next(
        (label for label, low, high in GAPS if low <= seconds < high), GAPS[0][0] if seconds < 0 else GAPS[-1][0]
    )


class ProviderCredentials:
    """Temporary S3 credentials per CMR provider: fetched once, and renewed before their hour is up."""

    def __init__(self) -> None:
        self._held: dict[str, tuple[float, dict[str, str]]] = {}

    def get(self, provider: str) -> dict[str, str]:
        import earthaccess

        fetched, credentials = self._held.get(provider, (0.0, {}))
        if not credentials or time.monotonic() - fetched > CREDENTIALS_RENEW_AFTER_S:
            earthaccess.login()
            credentials = earthaccess.get_s3_credentials(provider=provider)
            self._held[provider] = (time.monotonic(), credentials)
        return credentials


def s3_store(bucket: str, region: str = "us-west-2", credentials: dict[str, str] | None = None) -> Any:
    """An obstore S3 store for ``bucket``; unsigned requests when no credentials are given."""
    from obstore.store import S3Store

    if credentials is None:
        return S3Store(bucket, region=region, skip_signature=True)
    return S3Store(
        bucket,
        region=region,
        access_key_id=credentials["accessKeyId"],
        secret_access_key=credentials["secretAccessKey"],
        token=credentials["sessionToken"],
    )
