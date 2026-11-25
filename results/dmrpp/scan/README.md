# Every sidecar of the Int16 collections

`summary.csv` has one row per collection scanned by `python -m dmrpp.scan` in
us-west-2 on 2026-11-24: the 39 PO.DAAC collections where the verifier found
signed 8-bit variables declared `Int16` (OPENDAP/hyrax#17).

```sh
uv run --extra dmrpp python -m dmrpp.scan --int16-findings results/dmrpp/verify/verdicts.csv --out scan
```

A sidecar is `flagged` when it has no `dmrpp:version` attribute and has
`fullnamepath` attributes on its variables, the mark every faulty sidecar
found so far carries; any other is `clear`. `clear_unversioned` counts the
clear ones that have no version either. The scan reads the first 8 KB of
every sidecar under the collection's S3 prefix. It does not compare a sidecar
with its file: the verifier did that for one or two granules per collection.

The per-sidecar lists the scan writes (`sidecars/<short_name>.csv.gz`, one
row per sidecar) are not here: they have 10.9 million rows.

## What it found

- 10,880,248 sidecars in the 39 collections: 6,272,975 flagged, 4,607,266
  clear, 6 that are not DMR++ documents and 1 that could not be read (all 7
  in `VIIRS_NPP-JPL-L2P-v2016.2`).
- 14 collections are flagged throughout (2,427,988 sidecars).
- Five collections hold 82% of the flagged sidecars:
  `VIIRS_NPP-NAVO-L2P-v3.0` (1,340,259), `MODIS_T-JPL-L2P-v2019.0`
  (1,106,329), `VIIRS_NPP-JPL-L2P-v2016.2` (999,226),
  `VIIRS_NPP-NAVO-L2P-v1.0` (980,051) and `VIIRS_NPP-NAVO-L2P-v2.0`
  (715,052).
- Every flagged sidecar was last modified between 2024-07-17T02:14 and
  2024-07-18T02:23 UTC.
- The scan took 2 hours 33 minutes at 128 reads in flight.

## Against the border search

`results/dmrpp/border/` estimated the same thing from about 30 sidecars per
collection. For 19 of the 23 collections it called a single step, the scan's
flagged count is exactly the border search's `granules_old_side`. The other
four are lower by 2 (`MUR25-JPL-L4-GLOB-v04.2`), 6
(`SEVIRI_IO_SST-OSISAF-L3C-v1.0`), 323 (`VIIRS_NPP-NAVO-L2P-v3.0`) and 2,309
(`VIIRS_NPP-JPL-L2P-v2016.2`). Each of the 14 it called flagged throughout
has no clear sidecar.

A sidecar count can differ from CMR's granule count by a few either way. The
two Jason-1 collections each have two more sidecars than CMR has granules;
in `JASON-1_L2_OST_GPN_E` they are for cycle 310, passes 142 and 165.
