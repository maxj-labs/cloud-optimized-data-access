# The cloud archive inventory

`cmr_cloud_inventory.csv` has one row per cloud-hosted CMR collection (9,577,
from CMR on 2026-11-05): provider, format family, processing level, granule
count and an estimated size. It is the input to the DMR++ survey
(`python -m dmrpp.survey run --inventory benchmarks/cost/data/cmr_cloud_inventory.csv`),
which samples the 5,617 rows that are HDF-family collections with granules.

`scripts/cmr_cloud_inventory.py` rebuilds it from CMR's metadata catalog
alone: no data-server traffic and no login, one worker, rate limited. Its
docstring lists the stages. Two of them read the other files here:

| File | What | Source |
|---|---|---|
| `ems_fy2026.json` | Per-provider archive volume, growth and distribution, and the ten most-distributed products. The `resample` stage uses the product list to sample those collections more thoroughly. | EOSDIS FY2026 Annual Metrics Report (EMS), earthdata.nasa.gov/about/data-metrics/annual-metrics-reports |
| `virtualizability_scores.csv` | 86 collections graded by opening their granules. The `merge` stage joins the grades into the inventory. | alex-s-gardner/EarthDataVirtualizabilityScore (MIT) |

Known gap: the script uses CMR's normal search, which leaves out deprecated
collections, so the inventory does too.

The sizes and grades are here because a cost model was built on this
inventory. That model is not in this repository yet; only what the survey
needs is.
