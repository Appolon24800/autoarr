# Rust 0.5 vs Python 0.4

Measured 2026-10-27T00:43:42.401944+00:00 on Linux x86_64; Python 3.14.8.

Both versions ran as fresh CLI processes against the same synthetic local fixtures. Medians use 11 timed samples after two warm-ups, alternating execution order. Build time is excluded.

| Scenario | Python median | Rust median | Speedup | Native requests |
| --- | ---: | ---: | ---: | ---: |
| Process startup (--version) | 46.220 ms | 0.476 ms | 97.10× | 0 |
| Filter 266 saved releases | 54.921 ms | 3.500 ms | 15.69× | 0 |
| One API read | 54.722 ms | 2.003 ms | 27.32× | 1 |
| 20 reads, four workers | 170.901 ms | 4.835 ms | 35.35× | 20 |
| Three dependent steps | 71.406 ms | 2.784 ms | 25.65× | 2 |
| 20 reads, 8 ms API delay | 213.079 ms | 44.821 ms | 4.75× | 20 |

All non-version scenarios verify equivalent JSON results after excluding cache identifiers and timestamps, and assert exactly the same API request counts. Version output intentionally differs. Cache state starts empty for each timed sample. The dependent workflow includes result caching; other scenarios disable it. The release fixture contains 266 synthetic records. The latency scenario adds 8 ms to each local API response.

Release executable: 6,348,624 bytes (6.05 MiB).

These are local CLI timings, not download speed, internet latency, or end-to-end LLM task time. Real server/network latency limits the benefit, as shown by the 8 ms scenario.

Validation: 15 Rust unit tests, 90 native executable integration tests, and 129 Python reference tests passed. Strict Clippy and formatting checks passed. Read-only live checks succeeded for Sonarr, qBittorrent, and Jellyfin; no media writes, grabs, scans, imports, or SSH operations were used.

Reproduce:

```sh
cargo build --release --locked
python3 scripts/benchmark_rust.py --samples 11 --warmups 2 --output docs/benchmarks/rust-vs-python.json
```

[Raw measurements, p95 latencies, and methodology](rust-vs-python.json).
