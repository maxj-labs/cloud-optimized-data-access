# Where the sidecars change kind

`borders.csv` has one row per collection searched by `python -m dmrpp.border`,
and `probes.jsonl` one row per sidecar it read on the way. The run of
2026-11-24 searched the 39 PO.DAAC collections where the verifier found
signed 8-bit variables declared `Int16` (OPENDAP/hyrax#17). Those sidecars
have no `dmrpp:version` attribute, so the search looks for where sidecars
without the attribute ("unversioned") give way to sidecars with it.

```sh
EARTHDATA_TOKEN=... uv run --extra dmrpp python -m dmrpp.border --int16-findings results/dmrpp/verify/verdicts.csv
```

A rerun keeps the rows it has and searches again only where a search failed.

## Reading it

| Verdict | Collections | What it means |
|---|---:|---|
| `step` | 22 | The oldest sidecar is unversioned and the newest versioned, the search found two neighbouring granules where the kind changes, and every granule checked on each side agrees with its side. |
| `all_unversioned` | 15 | The oldest and the newest sidecar are both unversioned, and so are 20 granules spaced evenly in time between them. |
| `not_a_step` | 2 | The search found a place where the kind changes, but granules checked on one side or both are of the other kind. `disagreeing` lists them. |

The check behind a `step` is `old_side_checked` and `new_side_checked`
sidecars: up to 5 next to the border and up to 10 spaced evenly in time on
each side. It is a sample. An island of the other kind that falls between the
granules checked is not seen.

`granules_old_side` is CMR's count of granules that start no later than
`last_old_granule`. For a `step` it is the number of unversioned sidecars if
the step holds everywhere. For a `not_a_step` it is not a count of anything.

## What the run found

- The 22 steps have 2,888,596 granules on the unversioned side of 5,221,907.
  In 20 of them the border falls between 2022-01-09 and 2022-02-09.
- The 15 collections that are unversioned throughout have 2,434,250 granules.
- `JASON_CS_S6A_L2_ALT_LR_RED_OST_NRT_F`: the search stopped at one versioned
  sidecar (2022-02-08T01:55, written in 2023) with unversioned sidecars either
  side of it. The sidecars read from 2022-02-10 on are all versioned, as in
  the other three Sentinel-6 collections, so the border is probably within two
  days of the one reported.
- `MODIS_T-JPL-L2P-v2019.0` (3,204,773 granules) has no single border. Of the
  sidecars read, those from 2003 to 2009 are versioned, those from 2011 to
  mid-2020 unversioned, and one from 2020-11 unversioned among versioned ones.
  Counting its unversioned sidecars needs every sidecar read.

Whether an unversioned sidecar declares the wrong type was checked by the
verifier on one or two granules per collection, not here.
