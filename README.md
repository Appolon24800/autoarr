<img src="assets/icon.svg" width="64" height="64" alt="">

# AutoARR

Created in [T3 Code](https://t3.codes).

## CLI

`autoarr` is a native Rust CLI that controls the stack through each application's supported API. Version 0.5 ports the CLI, HTTP/authentication, discovery, compact output and filters, private result cache, dependent batches, waits, and media workflows to Rust. Python is retained as a test and benchmark reference; the normal executable does not invoke it. Normal operations do not use SSH.

```sh
cargo build --release --locked
./bin/autoarr --version
# Optional installation of the native executable:
cargo install --path . --locked
```

```sh
./bin/autoarr doctor
./bin/autoarr batch --apps sonarr,radarr,lidarr -- queue --all
./bin/autoarr sonarr find-missing 'American Horror Story' --vostfr
./bin/autoarr result --id RESULT_ID --filter 'seeders>=5' --sort=-seeders --limit 3
./bin/autoarr radarr ops --search import --details
./bin/autoarr jellyfin items --type Series --search Monogatari

# Follow an existing download through import before continuing the task.
./bin/autoarr sonarr wait --download-id DOWNLOAD_ID --wait-timeout 3600
# Or attach waiting to an explicitly chosen, authorized release grab.
./bin/autoarr sonarr grab --ref RESULT_ID:ROW_NUMBER --wait --wait-timeout 3600
```

Supported applications include Radarr, Sonarr, Lidarr, Prowlarr, Bazarr, Bookshelf, Jellyfin, Seerr, Kapowarr, qBittorrent, Navidrome, Autobrr, and Cleanuparr. Configure your own connections before running application commands. The compiled executable needs no Python interpreter.

Default responses show relevant fields, up to 10 rows and 6,000 characters, with counts and a private cached result ID. `--full --pretty` returns native details. `find-missing` compares releases without starting downloads; VOSTFR labels are reported separately from verified subtitles.

Wait progress streams to stderr; stdout contains one final JSON response. Timeouts and interruptions return a read-only resume command without repeating the action. This keeps a waiting tool alive; waking an agent session that has ended requires its host's scheduler or event integration. See [waiting and agent continuation](docs/cli.md#waiting-and-agent-continuation).

For fewer agent turns, a named batch can read an item, verify exactly one match, pass its native ID to an authorized follow-up, and report the result in one tool call. See [dependent steps and checks](docs/cli.md#dependent-steps-and-checks).

Run `python3 scripts/benchmark_rust.py --samples 11` to compare complete Rust and Python CLI processes against identical local fixtures. The benchmark never contacts configured apps or trackers. Results cover startup, offline filtering, single reads, 20-read batches, dependent workflows, and simulated API latency. Python 0.4 is available explicitly as `./bin/autoarr-python` for reference.

The measured rewrite reduced startup from **46.22 ms to 0.48 ms**, a 20-read batch from **170.90 ms to 4.84 ms**, and a three-step dependent workflow from **71.41 ms to 2.78 ms**. With an added 8 ms of API latency per request, the batch improved **4.75×**. See the [full benchmark report](docs/benchmarks/rust-vs-python.md) and [raw measurements](docs/benchmarks/rust-vs-python.json).

[Full command guide, examples, setup, and verification](docs/cli.md).

## Configuration

```sh
./bin/autoarr config add sonarr --kind sonarr \
  --url https://sonarr.example.invalid --api-key-env SONARR_API_KEY
```

Replace the example URL with your application's address and supply `SONARR_API_KEY` through your private environment. See `config.example.json` for more connection examples. Configuration and cached API responses belong in the ignored `.local/autoarr/` directory. Keep credentials out of command-line arguments and Git.

This repository excludes personal server audits, connection files, request logs, and media-library records.
