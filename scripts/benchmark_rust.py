"""Compare complete Python/Rust CLI processes against identical local fixtures.

No configured app, tracker, credential, SSH connection, or real download is used.
Build first: cargo build --release --locked
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


class LocalAPI(BaseHTTPRequestHandler):
    calls = 0
    request_delay = 0
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def do_GET(self):
        with self.lock:
            type(self).calls += 1
        if self.request_delay:
            time.sleep(self.request_delay)
        path = urlsplit(self.path).path
        data = {'id': int(path.rsplit('/', 1)[-1]), 'title': 'Benchmark', 'monitored': True} if '/series/' in path else {'version': 'fixture', 'ok': True}
        payload = json.dumps(data, separators=(',', ':')).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.5))]


def comparable(value):
    """Ignore cache identifiers/timestamps, while comparing actual CLI results."""
    if isinstance(value, dict):
        return {k: comparable(v) for k, v in value.items() if k not in ('result', 'cachedAt')}
    if isinstance(value, list):
        return [comparable(v) for v in value]
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=int, default=11)
    parser.add_argument('--warmups', type=int, default=2)
    parser.add_argument('--rust-bin', type=Path, default=ROOT / 'target/release/autoarr')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not 1 <= args.samples <= 100 or not 0 <= args.warmups <= 20:
        parser.error('--samples must be 1–100, --warmups 0–20')
    if not args.rust_bin.is_file():
        parser.error('Build the release binary first: cargo build --release --locked')
    runtimes = {'python': [sys.executable, str(ROOT / 'bin/autoarr-python')], 'rust': [str(args.rust_bin.resolve())]}
    versions = {name: subprocess.check_output([*cmd, '--version'], text=True).strip() for name, cmd in runtimes.items()}
    report = {'createdAt': datetime.now(timezone.utc).isoformat(), 'versions': versions,
              'platform': {'os': platform.system(), 'architecture': platform.machine(), 'python': platform.python_version()},
              'samples': args.samples, 'warmups': args.warmups, 'rustBinaryBytes': args.rust_bin.stat().st_size,
              'measurement': 'Complete fresh CLI processes, wall-clock latency; alternating runtime order; release build; local synthetic HTTP/data only.',
              'scenarios': []}
    server = ThreadingHTTPServer(('127.0.0.1', 0), LocalAPI)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='autoarr-benchmark-') as directory:
            directory = Path(directory)
            configs = {}
            for name in runtimes:
                state = directory / name
                state.mkdir(mode=0o700)
                config = state / 'config.json'
                config.write_text(json.dumps({'version': 1, 'apps': {'sonarr': {'kind': 'sonarr', 'url': f'http://127.0.0.1:{server.server_port}', 'auth': 'none'}}}))
                config.chmod(0o600)
                configs[name] = config
            data = directory / 'releases.json'
            data.write_text(json.dumps([{'title': f'Fixture.S01E{i % 20 + 1:02}.VOSTFR.{i}', 'guid': f'fixture-{i}',
                'indexer': 'Local fixture', 'indexerId': 1, 'seeders': i % 53, 'size': 2**30, 'episodeNumbers': [i % 20 + 1],
                'quality': {'quality': {'name': 'WEBDL-1080p'}}, 'rejections': [], 'customFormatScore': i % 3,
                'downloadUrl': 'https://example.invalid/not-a-download/' + 'x' * 500} for i in range(266)]))
            task = json.dumps([
                {'name': 'show', 'argv': ['sonarr', 'get', 'series', '7'], 'expect': ['data.id=7']},
                ['sonarr', 'get', 'series', '{steps.show.data.id}'],
                ['result', '--id', '{steps.show.result}', '--select', 'id,title']])
            scenarios = [
                ('startup', ['--version'], 0, 0, False),
                ('offline_filter_266_releases', ['--no-cache', 'result', '--file', str(data), '--filter', 'seeders>=5', '--sort=-seeders', '--limit', '5'], 0, 0, False),
                ('single_read', ['--no-cache', 'sonarr', 'get', 'series', '7'], 1, 0, False),
                ('batch_20_reads', ['--no-cache', 'batch', '--ids', '1-20', '--', 'sonarr', 'get', 'series', '{id}'], 20, 0, False),
                ('dependent_3_steps', ['batch', '--file', task], 2, 0, True),
                ('batch_20_reads_8ms_api', ['--no-cache', 'batch', '--ids', '1-20', '--', 'sonarr', 'get', 'series', '{id}'], 20, 0.008, False),
            ]
            for label, argv, expected_requests, delay, cache in scenarios:
                LocalAPI.request_delay = delay
                measurements = {name: [] for name in runtimes}
                outputs = {}
                for iteration in range(args.warmups + args.samples):
                    names = list(runtimes) if iteration % 2 == 0 else list(reversed(runtimes))
                    results = {}
                    for name in names:
                        # Both engines get the same empty cache at each sample.
                        shutil.rmtree(configs[name].parent / 'results', ignore_errors=True)
                        command = [*runtimes[name], '--config', str(configs[name]), *argv]
                        before = LocalAPI.calls
                        started = time.perf_counter_ns()
                        process = subprocess.run(command, capture_output=True, text=True, timeout=30)
                        elapsed = (time.perf_counter_ns() - started) / 1_000_000
                        if process.returncode:
                            raise RuntimeError(f'{label}/{name} failed: {process.stderr}')
                        if LocalAPI.calls - before != expected_requests:
                            raise AssertionError(f'{label}/{name}: incorrect native request count')
                        if label != 'startup':
                            parsed = json.loads(process.stdout)
                            results[name] = comparable(parsed)
                            if label.startswith('batch') and parsed['succeeded'] != 20:
                                raise AssertionError('A benchmark batch did not finish every read')
                            if label == 'dependent_3_steps' and parsed['succeeded'] != 3:
                                raise AssertionError('A dependent step did not complete')
                        outputs[name] = len(process.stdout.encode())
                        if iteration >= args.warmups:
                            measurements[name].append(elapsed)
                    if label != 'startup' and results['python'] != results['rust']:
                        raise AssertionError(f'{label}: Python/Rust output differs beyond cache metadata')
                scenario = {'name': label, 'nativeRequests': expected_requests, 'apiDelayMs': delay * 1000, 'resultCaching': cache}
                scenario['equivalentOutput'] = label != 'startup'
                for name, samples in measurements.items():
                    scenario[name] = {'medianMs': round(statistics.median(samples), 3), 'p95Ms': round(percentile(samples, 0.95), 3),
                                      'minMs': round(min(samples), 3), 'stdoutBytes': outputs[name]}
                scenario['speedup'] = round(scenario['python']['medianMs'] / scenario['rust']['medianMs'], 2)
                report['scenarios'].append(scenario)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, separators=(',', ':')))


if __name__ == '__main__':
    main()
