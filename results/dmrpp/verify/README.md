# Verifier verdicts

`verdicts.csv` has one row per granule: the newest verdict from every run of
`python -m dmrpp.verify` so far. Its last column, `run`, names the run a row
came from (a first pass per provider, `_redo` passes after fixes to the
verifier, and `values_` runs that also asked Hyrax for values). The other
columns and the meaning of each verdict are in `python -m dmrpp.verify --help`.

To add a run, write it to a file of its own and fold it in, oldest first:

```sh
uv run --extra dmrpp python -m dmrpp.verify results/dmrpp/survey/sampled_with_sidecars.csv \
    --provider LPCLOUD --redo results/dmrpp/verify/verdicts.csv --out results/dmrpp/verify/lpcloud_redo.csv
uv run python -m dmrpp.verify.merge results/dmrpp/verify/verdicts.csv \
    results/dmrpp/verify/verdicts.csv results/dmrpp/verify/lpcloud_redo.csv
```

`--redo` picks the granules that are not `match` or `skipped`, so it also
repeats the ones whose verdict is a real finding.

Not every row is a finding about a sidecar. Errors are mostly files or
sidecars the verifier's parsers cannot read, and some `MISMATCH` and
`METADATA_MISMATCH` rows are known to be the verifier's doing (HDF4 files in
MOD17 and MYD17, a variable shorter than its unlimited dimension in ATom).
