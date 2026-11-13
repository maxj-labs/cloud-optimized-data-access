# Trade-study run `goes19-1h-2026-11-12-workstation`

410 trials, 0 failed. Medians of cold runs, one fresh process each.

Client: **out of region** (client region: unknown, not AWS; host: other). Virtual-store index read from: local-disk. Discovery (s3-list): 0.15 s, measured when the granules were pinned; not in first byte.

## goes19: time to first byte, in parts (seconds)

| Case | Granules | Arm | n | Auth | Manifest | Chunk id | Bytes | **First byte** | min–max | Warm | Requests | MB on wire |
|---|---:|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|
| `batch:0:1h` | 6 | `virtual_icechunk` | 10 | 0.00 | 0.40 | — | 0.05 | **0.45** | 0.43–0.79 | 0.047 | ≥ 8 | 0.1 |
| `batch:0:1h` | 6 | `tuned_h5py` | 10 | 0.00 | 0.59 | 0.00 | 0.29 | **0.87** | 0.80–1.37 | 0.002 | 3 | 16.8 |
| `batch:0:1h` | 6 | `download` | 10 | 0.01 | 0.00 | — | 1.25 | **1.26** | 1.07–2.14 | 0.987 | 3 | 25.5 |
| `batch:0:1h` | 6 | `user_stack` | 10 | 0.00 | 7.28 | — | 0.07 | **7.40** | 7.11–8.14 | 0.032 | 12 | 153.7 |
| `box:100:1h` | 6 | `virtual_icechunk` | 10 | 0.00 | 0.41 | — | 0.09 | **0.51** | 0.47–0.99 | 0.052 | ≥ 8 | 0.2 |
| `box:100:1h` | 6 | `tuned_h5py` | 10 | 0.00 | 0.54 | 0.00 | 0.30 | **0.85** | 0.77–0.95 | 0.002 | 3 | 16.8 |
| `box:100:1h` | 6 | `download` | 10 | 0.01 | 0.00 | — | 1.25 | **1.26** | 1.07–1.70 | 0.960 | 3 | 25.5 |
| `box:100:1h` | 6 | `user_stack` | 10 | 0.00 | 7.30 | — | 0.06 | **7.35** | 7.20–7.90 | 0.051 | 12 | 153.7 |
| `churro:10:1h` | 6 | `virtual_icechunk` | 10 | 0.00 | 0.39 | — | 0.46 | **0.84** | 0.83–1.01 | 0.055 | ≥ 8 | 0.2 |
| `churro:10:1h` | 6 | `tuned_h5py` | 10 | 0.00 | 0.58 | 0.00 | 0.32 | **0.89** | 0.77–0.99 | 0.003 | 3 | 16.8 |
| `churro:10:1h` | 6 | `download` | 10 | 0.01 | 0.00 | — | 1.20 | **1.22** | 1.08–2.62 | 1.009 | 3 | 25.5 |
| `churro:10:1h` | 6 | `user_stack` | 10 | 0.00 | 7.52 | — | 0.12 | **7.59** | 7.07–8.84 | 0.053 | 12 | 153.1 |
| `churro:250:1h` | 6 | `virtual_icechunk` | 10 | 0.00 | 0.40 | — | 0.46 | **0.86** | 0.82–1.10 | 0.052 | ≥ 8 | 0.2 |
| `churro:250:1h` | 6 | `tuned_h5py` | 10 | 0.00 | 0.55 | 0.00 | 0.31 | **0.86** | 0.82–0.98 | 0.003 | 3 | 16.8 |
| `churro:250:1h` | 6 | `download` | 10 | 0.01 | 0.00 | — | 1.20 | **1.22** | 1.15–2.76 | 1.039 | 3 | 25.5 |
| `churro:250:1h` | 6 | `user_stack` | 10 | 0.00 | 7.36 | — | 0.03 | **7.42** | 6.97–9.71 | 0.020 | 12 | 153.7 |
| `pancake:1:1h` | 1 | `virtual_icechunk` | 10 | 0.00 | 0.39 | — | 0.05 | **0.44** | 0.42–0.70 | 0.046 | ≥ 8 | 0.1 |
| `pancake:1:1h` | 1 | `tuned_h5py` | 10 | 0.00 | 0.58 | 0.00 | 0.29 | **0.86** | 0.76–0.99 | 0.002 | 3 | 16.8 |
| `pancake:1:1h` | 1 | `download` | 10 | 0.01 | 0.00 | — | 1.18 | **1.20** | 1.10–1.66 | 0.987 | 3 | 25.5 |
| `pancake:1:1h` | 1 | `user_stack` | 10 | 0.00 | 1.91 | — | 0.01 | **1.93** | 1.80–2.17 | 0.009 | 2 | 25.6 |

## goes19: the whole question (seconds)

| Case | Granules | Arm | n | **End to end** | min–max | Last byte | Warm | Floor | × floor | Requests | MB on wire | Peak RSS MB | $ per question |
|---|---:|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `batch:0:1h` | 6 | `tuned_h5py` | 10 | **9.73** | 9.55–13.68 | 9.09 | 7.60 | 6.81 | 1.4 | 26 | 162.3 | 3,309.8 | 0.015146 |
| `batch:0:1h` | 6 | `download` | 10 | **9.92** | 8.47–12.30 | 9.23 | 9.03 | 6.62 | 1.5 | 18 | 153.7 | 3,375.8 | 0.014359 |
| `batch:0:1h` | 6 | `virtual_icechunk` | 10 | **10.39** | 10.08–10.72 | 9.73 | 9.36 | 6.93 | 1.5 | ≥ 1,363 | 157.4 | 3,368.8 | 0.015265 |
| `batch:0:1h` | 6 | `user_stack` | 10 | **13.45** | 12.93–20.87 | 12.85 | 5.40 | 6.40 | 2.1 | 12 | 153.7 | 3,968.9 | 0.014546 |
| `box:100:1h` | 6 | `virtual_icechunk` | 10 | **0.58** | 0.55–0.99 | 0.58 | 0.14 | 0.22 | 2.7 | ≥ 25 | 2.5 | 165.4 | 0.000265 |
| `box:100:1h` | 6 | `tuned_h5py` {"max_workers": 1} | 10 | **5.06** | 4.90–5.74 | 5.05 | 0.02 | 4.24 | 1.2 | 18 | 100.8 | 227.2 | 0.009351 |
| `box:100:1h` | 6 | `tuned_h5py` | 10 | **5.67** | 4.73–6.04 | 5.67 | 3.90 | 4.60 | 1.2 | 20 | 109.6 | 128.7 | 0.010175 |
| `box:100:1h` | 6 | `download` | 10 | **6.53** | 5.55–8.36 | 6.53 | 6.20 | 6.62 | 1.0 | 18 | 153.7 | 270.8 | 0.014149 |
| `box:100:1h` | 6 | `user_stack` | 10 | **7.50** | 7.08–8.17 | 7.50 | 0.07 | 6.40 | 1.2 | 12 | 153.1 | 368.7 | 0.014188 |
| `churro:10:1h` | 6 | `virtual_icechunk` | 10 | **1.06** | 1.01–1.20 | 1.06 | 0.25 | 0.31 | 3.5 | ≥ 43 | 4.6 | 194.9 | 0.000486 |
| `churro:10:1h` | 6 | `tuned_h5py` | 10 | **5.65** | 4.46–5.91 | 5.65 | 3.74 | 4.60 | 1.2 | 20 | 109.5 | 128.7 | 0.010160 |
| `churro:10:1h` | 6 | `download` | 10 | **6.20** | 5.63–7.06 | 6.20 | 6.13 | 6.62 | 0.9 | 18 | 153.0 | 281.9 | 0.014131 |
| `churro:10:1h` | 6 | `user_stack` | 10 | **7.58** | 7.28–10.37 | 7.58 | 0.14 | 6.40 | 1.2 | 12 | 153.7 | 367.1 | 0.014234 |
| `churro:250:1h` | 6 | `virtual_icechunk` | 10 | **1.25** | 1.16–2.30 | 1.25 | 0.35 | 0.39 | 3.2 | ≥ 61 | 6.6 | 197.4 | 0.000684 |
| `churro:250:1h` | 6 | `tuned_h5py` | 10 | **5.38** | 4.65–6.05 | 5.38 | 3.87 | 4.60 | 1.2 | 20 | 109.5 | 128.8 | 0.010156 |
| `churro:250:1h` | 6 | `download` | 10 | **6.30** | 5.77–7.05 | 6.30 | 6.19 | 6.62 | 1.0 | 18 | 153.3 | 278.7 | 0.014150 |
| `churro:250:1h` | 6 | `user_stack` | 10 | **7.77** | 7.23–39.54 | 7.77 | 0.22 | 6.40 | 1.2 | 12 | 153.7 | 372.5 | 0.014254 |
| `pancake:1:1h` | 1 | `tuned_h5py` | 10 | **1.65** | 1.61–1.79 | 1.54 | 0.36 | 1.14 | 1.4 | 4 | 25.5 | 707.7 | 0.002385 |
| `pancake:1:1h` | 1 | `download` | 10 | **1.69** | 1.62–2.10 | 1.59 | 1.43 | 1.10 | 1.5 | 3 | 25.5 | 732.6 | 0.002387 |
| `pancake:1:1h` | 1 | `virtual_icechunk` | 10 | **2.19** | 2.09–2.76 | 2.07 | 1.61 | 1.26 | 1.7 | ≥ 233 | 26.5 | 710.1 | 0.002595 |
| `pancake:1:1h` | 1 | `user_stack` | 10 | **2.89** | 2.77–3.91 | 2.78 | 1.00 | 1.10 | 2.6 | 2 | 25.4 | 780.8 | 0.002448 |

## Reading the tables

- A dash under a part means the arm does not expose that step; its time is inside Bytes:
  - `download`: no chunk selection: the whole file is fetched.
  - `download`: no index: the whole file is fetched.
  - `tuned_h5py`: inside the read: each worker process looks up its own granule's chunks.
  - `user_stack`: inside the read: h5netcdf resolves chunks as it reads.
  - `virtual_icechunk`: inside the read: Icechunk fetches the chunk manifest on the first chunk request.
- Requests marked ≥ are a static minimum from the chunk grid, not a count. Other counts are byte ranges the client asked for.
- MB on wire is the host's receive counter over the trial, so it includes other traffic on a shared host.
- Floor = dependent round trips × median round-trip time + bytes ÷ measured bandwidth, from the session's probe.
- $ per question = S3 requests + transfer + compute at list price. Transfer is zero in region and internet egress otherwise.
- Arms marked † are not gated: their values were not checked against the reference arm.
