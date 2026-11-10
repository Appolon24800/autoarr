"""Measure Python-reference CLI overhead; use benchmark_rust.py for Rust comparisons."""
import argparse
import contextlib
import io
import json
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from autoarr import __version__
from autoarr.cli import main as cli, parse
from autoarr.config import private_json


class LocalAPI(BaseHTTPRequestHandler):
    calls = 0
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def do_GET(self):
        with self.lock:
            type(self).calls += 1
        payload = b'{"id":7,"title":"Benchmark"}'
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def median_ms(function, count):
    samples = []
    for _ in range(count):
        start = time.perf_counter()
        function()
        samples.append((time.perf_counter() - start) * 1000)
    return round(statistics.median(samples), 2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=int, default=5)
    parser.add_argument('--jobs', type=int, default=20)
    parser.add_argument('--baseline', type=Path, help='compare against a saved report')
    parser.add_argument('--output', type=Path, help='save a private report')
    args = parser.parse_args()
    if not 1 <= args.samples <= 100 or not 1 <= args.jobs <= 1000:
        parser.error('--samples must be 1–100 and --jobs 1–1000')
    report = {'version': __version__, 'samples': args.samples, 'batchJobs': args.jobs,
              'mode': 'local HTTP, no result cache'}
    report['parseMedianMs'] = median_ms(lambda: parse(['tv', 'queue'], {'apps': {'tv': {'kind': 'sonarr'}}}), args.samples)
    report['processVersionMedianMs'] = median_ms(lambda: subprocess.run(
        [sys.executable, str(ROOT / 'bin/autoarr-python'), '--version'], check=True, stdout=subprocess.DEVNULL), args.samples)
    server = ThreadingHTTPServer(('127.0.0.1', 0), LocalAPI)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            private_json(config, {'version': 1, 'apps': {'sonarr': {
                'kind': 'sonarr', 'url': f'http://127.0.0.1:{server.server_port}', 'auth': 'none'}}})
            argv = ['--config', str(config), '--no-cache', 'batch', '--ids', ','.join(['7'] * args.jobs),
                    '--', 'sonarr', 'get', 'series', '{id}']
            def batch():
                before = LocalAPI.calls
                with contextlib.redirect_stdout(io.StringIO()):
                    if cli(argv) != 0:
                        raise RuntimeError('Batch benchmark failed')
                if LocalAPI.calls - before != args.jobs:
                    raise RuntimeError('Unexpected API request count')
            report[f'batch{args.jobs}MedianMs'] = median_ms(batch, args.samples)
            report['apiRequestsPerBatch'] = args.jobs
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    if args.baseline:
        baseline = json.loads(args.baseline.read_text())
        report['speedup'] = {key: round(baseline[key] / value, 2) for key, value in report.items()
                             if key.endswith('MedianMs') and key in baseline and value > 0}
    if args.output:
        private_json(args.output, report)
    print(json.dumps(report, separators=(',', ':')))


if __name__ == '__main__':
    main()
