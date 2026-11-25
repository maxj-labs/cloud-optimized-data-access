# Where the flagged sidecars stop

`borders.csv` has one row per collection searched by `python -m dmrpp.border`,
and `probes.jsonl` one row per sidecar it read on the way. The run of
2026-11-24 searched the 39 PO.DAAC collections where the verifier found
signed 8-bit variables declared `Int16` (OPENDAP/hyrax#17).

A sidecar is "flagged" when it has no `dmrpp:version` attribute and has
`fullnamepath` attributes on its variables: the mark every faulty sidecar
found so far carries. Any other sidecar is "clear". A flag is not a
comparison with the file; the verifier made that comparison on one or two
granules per collection.

```sh
EARTHDATA_TOKEN=... uv run --extra dmrpp python -m dmrpp.border --int16-findings results/dmrpp/verify/verdicts.csv
```

A rerun keeps the rows it has and searches again only where a search failed.

## Reading it

| Verdict | Collections | What it means |
|---|---:|---|
| `step` | 23 | The oldest sidecar is flagged and the newest clear, the search found two neighbouring granules where that changes, and every granule checked on each side agrees with its side. |
| `all_flagged` | 14 | The oldest and the newest sidecar are both flagged, and so are 20 granules spaced evenly in time between them. |
| `not_a_step` | 2 | The search found a place where the flag changes, but granules checked on one side or both are of the other kind. `disagreeing` lists them. |

The check behind a `step` is `old_side_checked` and `new_side_checked`
sidecars: up to 5 next to the border and up to 10 spaced evenly in time on
each side. It is a sample. An island of the other kind that falls between the
granules checked is not seen; `python -m dmrpp.scan` reads every sidecar.

`granules_old_side` is CMR's count of granules that start no later than
`last_old_granule`. For a `step` it is the number of flagged sidecars if the
step holds everywhere. For a `not_a_step` it is not a count of anything.

## What the run found

- The 23 steps have 2,735,686 granules on the flagged side of 5,249,124. In
  21 of them the border falls between 2021-09-30 and 2021-11-19; the other
  two are `SEVIRI_SST-OSISAF-L3C-v1.0` (2017-12-31) and `MSG03-OSPO-L2P-v1.0`
  (2018-02-20).
- The 14 collections flagged throughout have 2,427,171 granules.
- `RAMSSA_09km-ABOM-L4-AUS-v01`: a border at 2021-11-18 with 5,638 granules
  before it, but one sidecar checked on the flagged side (2019-07-26) is
  clear.
- `MODIS_T-JPL-L2P-v2019.0` (3,204,773 granules) has no single border. Of the
  sidecars read, those from 2003 to 2009 are clear, those from 2011 to
  mid-2020 flagged, and one from 2020-11 flagged among clear ones.

## The flag is both conditions

A first run on the same day flagged a sidecar for having no `dmrpp:version`
alone. It put most borders about three months later, in early February 2022,
and called `AVHRRMTA_G-NAVO-L2P-v2.0` unversioned throughout. Sidecars
written for granules of those months have no version and no `fullnamepath`
attributes, and the one the verifier compared with its file
(`AVHRRMTA_G-NAVO-L2P-v2.0`, 2021-11-27) declares `Int8` correctly. One
flagged and one clear sidecar per collection were also read whole: every
flagged one has no `Int8` variable at all, and every clear one has some.
