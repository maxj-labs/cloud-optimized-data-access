# Every sidecar of the Int16 collections

`summary.csv` has one row per collection scanned by `python -m dmrpp.scan` in
us-west-2 on 2026-11-24 and 25: the 53 PO.DAAC collections where the verifier
found signed 8-bit variables declared `Int16`. The first 39 rows are the
collections listed in OPENDAP/hyrax#17; the last 14 are the ones the verifier
added once the survey had reached the rest of PO.DAAC.

```sh
uv run --extra dmrpp python -m dmrpp.scan --int16-findings results/dmrpp/verify/verdicts.csv --out scan
```

A sidecar is `flagged` when it has no `dmrpp:version` attribute and has
`fullnamepath` attributes on its variables, the mark every faulty sidecar
found so far carries; any other is `clear`. `clear_unversioned` counts the
clear ones that have no version either. The scan reads the first 8 KB of
every sidecar under the collection's S3 prefix. It does not compare a sidecar
with its file: the verifier did that for one or two granules per collection,
104 in all, and each of the 71 flagged sidecars among them declares every
signed 8-bit variable of its file as `Int16`.

The per-sidecar lists the scan writes (`sidecars/<short_name>.csv.gz`, one
row per sidecar) are not here: they have 14 million rows.

## What it found

| | Collections | Sidecars | Flagged |
|---|---:|---:|---:|
| Listed in hyrax#17 | 39 | 10,880,248 | 6,272,975 |
| Added on 2026-11-24 | 14 | 3,195,348 | 2,374,257 |
| All | 53 | 14,075,596 | 8,647,232 |

- Of the rest, 5,428,357 are clear, 6 are not DMR++ documents and 1 could
  not be read (all 7 in `VIIRS_NPP-JPL-L2P-v2016.2`).
- 20 collections are flagged throughout (2,533,073 sidecars).
- Six collections hold 85% of the flagged sidecars:
  `MODIS_A-JPL-L2P-v2019.0` (2,232,058), `VIIRS_NPP-NAVO-L2P-v3.0`
  (1,340,259), `MODIS_T-JPL-L2P-v2019.0` (1,106,329),
  `VIIRS_NPP-JPL-L2P-v2016.2` (999,226), `VIIRS_NPP-NAVO-L2P-v1.0` (980,051)
  and `VIIRS_NPP-NAVO-L2P-v2.0` (715,052).
- Every flagged sidecar was last modified between 2024-07-17T02:14 and
  2024-07-18T02:23 UTC.
- The scans took 3 hours 14 minutes in all at 128 reads in flight.

## Against the border search

`results/dmrpp/border/` estimated the same thing for the first 39
collections from about 30 sidecars each. For 19 of the 23 collections it
called a single step, the scan's flagged count is exactly the border search's
`granules_old_side`. The other four are lower by 2
(`MUR25-JPL-L4-GLOB-v04.2`), 6 (`SEVIRI_IO_SST-OSISAF-L3C-v1.0`), 323
(`VIIRS_NPP-NAVO-L2P-v3.0`) and 2,309 (`VIIRS_NPP-JPL-L2P-v2016.2`). Each of
the 14 it called flagged throughout has no clear sidecar.

A sidecar count can differ from CMR's granule count by a few either way. The
Jason-1 collections each have two more sidecars than CMR has granules; in
`JASON-1_L2_OST_GPN_E` they are for cycle 310, passes 142 and 165.
