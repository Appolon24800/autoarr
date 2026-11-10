"""Private, bounded snapshots so follow-up queries do not repeat API searches."""
import datetime
import json
import re
import secrets
import time
import threading

from .config import AutoarrError, private_json
from .output import records

_LOCK = threading.Lock()


class Results:
    def __init__(self, state_dir, ttl=86400):
        self.directory = state_dir / 'results'
        self.ttl = ttl

    def save(self, data, app=None, action=None):
        key = secrets.token_hex(6)
        entry = {'id': key, 'app': app, 'action': action,
                 'createdAt': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'data': data}
        private_json(self.directory / (key + '.json'), entry)
        with _LOCK:
            self.prune()
        return key

    def load(self, key):
        if not re.fullmatch('[0-9a-f]{12}', key):
            raise AutoarrError('Result IDs are 12 hexadecimal characters')
        file = self.directory / (key + '.json')
        if not file.exists() or file.is_symlink():
            raise AutoarrError('Result not found; repeat the source query')
        if time.time() - file.stat().st_mtime > self.ttl:
            raise AutoarrError('Result expired (24 hours); repeat the source query')
        return json.loads(file.read_text())

    def reference(self, ref, app, actions=('releases',)):
        try:
            key, index = ref.split(':')
            index = int(index)
        except ValueError:
            raise AutoarrError('A row reference has the form RESULT_ID:ROW_NUMBER') from None
        entry = self.load(key)
        if entry['app'] != app or entry['action'] not in actions:
            raise AutoarrError('Reference is not a release result for this app')
        rows = records(entry['data'])
        if rows is None or not 0 <= index < len(rows):
            raise AutoarrError('Reference row does not exist')
        return rows[index]

    def prune(self):
        files = []
        for file in self.directory.glob('*.json'):
            try:
                files.append((file.stat().st_mtime, file))
            except FileNotFoundError:
                pass
        files.sort(key=lambda pair: pair[0], reverse=True)
        size = 0
        now = time.time()
        for i, (_, file) in enumerate(files):
            if file.is_symlink():
                continue
            try:
                stat = file.stat()
            except FileNotFoundError:
                continue
            size += stat.st_size
            if i >= 100 or size > 128 * 1024 ** 2 or now - stat.st_mtime > self.ttl:
                file.unlink(missing_ok=True)
