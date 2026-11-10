"""Read-only, resumable polling for work owned by the native applications."""
import copy
import json
import math
import sys
import time
from urllib.parse import quote

from .client import ApiError
from .config import ARR, MANAGERS, AutoarrError
from .output import compact_value, predicate, records


def validate_options(timeout, interval):
    if not math.isfinite(timeout) or timeout < 0:
        raise AutoarrError('--wait-timeout must be finite and nonnegative')
    if not math.isfinite(interval) or interval <= 0:
        raise AutoarrError('--poll-interval must be finite and positive')


def validate_target(client, target):
    if not isinstance(target, dict):
        raise AutoarrError('A wait target must be a JSON object')
    kind = target.get('type')
    supported = {'command': ARR, 'download': MANAGERS, 'release': MANAGERS,
                 'torrent': {'qbittorrent'}, 'task': {'jellyfin'}}
    if kind == 'condition':
        if not isinstance(target.get('path'), str):
            raise AutoarrError('A condition wait needs --path')
        client.url(target['path'])
        # These OpenSubsonic GET operations are writes, even though they use GET.
        endpoint = target['path'].split('?')[0].rsplit('/', 1)[-1]
        if client.kind == 'navidrome' and endpoint.startswith((
                'create', 'update', 'delete', 'set', 'change', 'start', 'star',
                'unstar', 'scrobble', 'jukeboxControl')):
            raise AutoarrError('Wait conditions must use a read-only endpoint')
        if not isinstance(target.get('until'), list) or not target['until']:
            raise AutoarrError('A condition wait needs at least one --until expression')
        for key in ('until', 'failIf'):
            expressions = target.get(key, [])
            if not isinstance(expressions, list) or not all(isinstance(x, str) for x in expressions):
                raise AutoarrError('Wait conditions must be arrays of filter expressions')
            for expression in expressions:
                predicate(expression)
        query = target.get('query', [])
        if not isinstance(query, list) or any(not isinstance(pair, (list, tuple)) or len(pair) != 2 for pair in query):
            raise AutoarrError('Wait query must contain KEY=VALUE pairs')
    elif kind in supported:
        if client.kind not in supported[kind]:
            raise AutoarrError(f'{kind} waits are not supported for {client.kind}; use a condition wait')
        if kind == 'release':
            if not all(isinstance(target.get(key), str) and target[key] for key in ('title', 'indexer')):
                raise AutoarrError('Tracking a grabbed release needs its exact title and indexer name')
            if not isinstance(target.get('afterHistoryId'), int) or target['afterHistoryId'] < 0:
                raise AutoarrError('Tracking a grabbed release needs a history boundary')
        elif not isinstance(target.get('id'), (str, int)) or not str(target['id']).strip():
            raise AutoarrError('A wait needs an explicit target ID')
        if kind in ('download', 'release') and target.get('goal', 'imported') not in ('downloaded', 'imported'):
            raise AutoarrError('Download waits finish at downloaded or imported')
    else:
        raise AutoarrError('Unknown wait target type')
    if kind == 'task' and 'baseline' in target and target['baseline'] is not None and not isinstance(target['baseline'], dict):
        raise AutoarrError('A task baseline must be a native execution result')


def history(client, after_id=None, **query):
    from .workflows import paginated
    if after_id is not None:
        # Release discovery needs only new grabs, not the entire library history.
        found = []
        for page in range(1, 10001):
            data = client.request('GET', '/history', query=list(query.items()) + [
                ('page', page), ('pageSize', 250), ('sortKey', 'date'), ('sortDirection', 'descending')])
            rows = records(data)
            if rows is None:
                raise AutoarrError('History API did not return records')
            found.extend(row for row in rows if row.get('id', 0) > after_id)
            if not rows or any(row.get('id', 0) <= after_id for row in rows):
                return found
            total = data.get('totalRecords') if isinstance(data, dict) else len(rows)
            if total is not None and page * 250 >= total:
                return found
        raise AutoarrError('Grab history pagination exceeded 10,000 pages')
    data = paginated(client, '/history', list(query.items()), all_pages=True)
    rows = records(data)
    if rows is None:
        raise AutoarrError('History API did not return records')
    return rows


def event(row, *names):
    return str(row.get('eventType', '')).casefold() in names


def progress(data):
    keys = ('id', 'title', 'name', 'status', 'state', 'trackedDownloadState',
            'trackedDownloadStatus', 'size', 'sizeleft', 'timeleft', 'progress',
            'dlspeed', 'eta', 'message', 'statusMessages', 'CurrentProgressPercentage')
    result = {key: data[key] for key in keys if key in data}
    if isinstance(data.get('size'), (int, float)) and data['size'] > 0 and isinstance(data.get('sizeleft'), (int, float)):
        result['percent'] = round(100 * max(0, min(1, 1 - data['sizeleft'] / data['size'])), 1)
    elif isinstance(data.get('progress'), (int, float)):
        result['percent'] = round(100 * data['progress'], 1)
    return compact_value(result)


def probe(client, target):
    kind = target['type']
    identifier = quote(str(target.get('id', '')), safe='')
    if kind == 'condition':
        data = client.request('GET', target['path'], query=target.get('query', []))
        rows = data if isinstance(data, list) else [data]
        failed = any(predicate(expr)(row) for expr in target.get('failIf', []) for row in rows)
        done = bool(rows) and all(predicate(expr)(row) for expr in target['until'] for row in rows)
        detail = progress(data) if isinstance(data, dict) else {'records': len(data)} if isinstance(data, list) else {}
        return ('failed' if failed else 'completed' if done else 'pending'), detail or {'state': 'checking condition'}, data
    if kind == 'command':
        data = client.request('GET', '/command/' + identifier)
        if not isinstance(data, dict):
            raise AutoarrError('Command API did not return an object')
        status = str(data.get('status', '')).casefold()
        state = 'completed' if status == 'completed' else 'failed' if status in (
            'failed', 'aborted', 'cancelled', 'orphaned') else 'pending'
        return state, progress(data), data
    if kind == 'torrent':
        data = client.request('GET', '/torrents/info', query=[('hashes', target['id'])])
        if not isinstance(data, list):
            raise AutoarrError('Torrent API did not return an array')
        matches = [row for row in data if str(row.get('hash', '')).casefold() == str(target['id']).casefold()]
        if len(matches) != 1:
            return 'pending', {'state': 'not found; disappearance is not completion'}, data
        row = matches[0]
        status = str(row.get('state', '')).casefold()
        done = (row.get('progress', 0) >= 1 and status in (
            'uploading', 'stalledup', 'forcedup', 'queuedup', 'pausedup', 'stoppedup'))
        state = 'failed' if status in ('error', 'missingfiles') else 'completed' if done else 'pending'
        return state, progress(row), row
    if kind == 'task':
        data = client.request('GET', '/ScheduledTasks/' + identifier)
        if not isinstance(data, dict):
            raise AutoarrError('Task API did not return an object')
        last = data.get('LastExecutionResult') or {}
        running = str(data.get('State', '')).casefold() in ('running', 'cancelling')
        if running and 'baseline' not in target:
            target['baseline'] = data.get('LastExecutionResult')
        fresh = 'baseline' not in target or bool(last) and last != target['baseline']
        status = str(last.get('Status', '')).casefold()
        state = 'pending'
        if not running and fresh:
            state = 'completed' if status == 'completed' else 'failed' if status in ('failed', 'cancelled', 'aborted') else 'pending'
        return state, {'state': data.get('State'), 'percent': data.get('CurrentProgressPercentage'),
                       'lastResult': compact_value(last)}, data
    if kind == 'release':
        rows = history(client, after_id=target['afterHistoryId'], eventType=1)
        matches = [row for row in rows if row.get('id', 0) > target['afterHistoryId']
                   and row.get('sourceTitle') == target['title']
                   and row.get('data', {}).get('indexer') == target['indexer']
                   and row.get('downloadId') and event(row, 'grabbed', '1')]
        ids = {str(row['downloadId']).casefold() for row in matches}
        if len(ids) > 1:
            return 'failed', {'state': 'ambiguous grab; inspect history before choosing a download ID'}, matches
        if not ids:
            return 'pending', {'state': 'waiting for grabbed release to appear in history'}, {}
        download_id = matches[0]['downloadId']
        goal = target.get('goal', 'imported')
        boundary = target['afterHistoryId']
        target.clear()
        target.update(type='download', id=download_id, goal=goal, afterHistoryId=boundary)
    from .workflows import paginated
    queue = paginated(client, '/queue', all_pages=True)
    rows = records(queue)
    if rows is None:
        raise AutoarrError('Queue API did not return records')
    download_id = str(target['id']).casefold()
    matches = [row for row in rows if str(row.get('downloadId', '')).casefold() == download_id]
    if matches:
        states = [str(row.get('trackedDownloadState', '')).casefold() for row in matches]
        statuses = [str(row.get('status', '')).casefold() for row in matches]
        failed = any(state in ('failed', 'failedpending', 'importblocked', 'ignored') for state in states)
        failed = failed or any(status in ('failed', 'warning') for status in statuses)
        failed = failed or any(str(row.get('trackedDownloadStatus', '')).casefold() == 'error' for row in matches)
        done = all(state == 'imported' for state in states)
        if target.get('goal') == 'downloaded':
            done = done or all(status == 'completed' for status in statuses)
        state = 'failed' if failed else 'completed' if done else 'pending'
        detail = progress(matches[0])
        if len(matches) > 1:
            detail['queueRecords'] = len(matches)
        return state, detail, matches
    rows = [row for row in history(client, downloadId=target['id'])
            if str(row.get('downloadId', '')).casefold() == download_id
            and row.get('id', 0) > target.get('afterHistoryId', -1)]
    imports = [row for row in rows if event(row, 'downloadfolderimported', '3')]
    failures = [row for row in rows if event(row, 'downloadfailed', 'downloadignored', '4', '7')]
    # A season pack can have several episode import events. Require every
    # explicitly grabbed episode to have an import, even after queue removal.
    grabbed = [row for row in rows if event(row, 'grabbed', '1')]
    expected = {row['episodeId'] for row in grabbed if row.get('episodeId')}
    imported = {row['episodeId'] for row in imports if row.get('episodeId')}
    done = bool(imports) and expected <= imported
    state = 'failed' if failures else 'completed' if done else 'pending'
    return state, {'state': 'imported' if done else 'failed' if failures else 'not in queue; waiting for import evidence'}, rows


class DeadlineReached(Exception):
    pass


class PollClient:
    """Bound every polling request by the remaining wait budget; never retry writes."""
    def __init__(self, client, deadline):
        self.client, self.deadline = client, deadline

    def __getattr__(self, key):
        return getattr(self.client, key)

    def request(self, method, path, **kwargs):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise DeadlineReached()
        timeout = self.client.timeout
        try:
            self.client.timeout = min(timeout, remaining)
            return self.client.request(method, path, **kwargs)
        finally:
            self.client.timeout = timeout


def wait_for(client, target, timeout=300, interval=5, quiet=False, config=None):
    validate_options(timeout, interval)
    validate_target(client, target)
    target = copy.deepcopy(target)
    started = time.monotonic()
    deadline = started + timeout
    polling = PollClient(client, deadline)
    polls, state, detail, response = 0, 'pending', {}, None
    previous, last_emitted = None, started
    try:
        while True:
            if time.monotonic() >= deadline:
                state = 'timeout'
                break
            polls += 1
            try:
                state, detail, response = probe(polling, target)
            except ApiError as exc:
                state = 'pending' if exc.status == 429 or exc.status >= 500 else 'failed'
                detail = {'state': 'API error', 'httpStatus': exc.status, 'message': str(exc)}
            except AutoarrError as exc:
                state = 'pending' if str(exc).startswith('Request failed for ') else 'failed'
                detail = {'state': 'API error', 'message': str(exc)}
            if not quiet:
                client.redactor.collect(detail)
                clean = client.redactor.clean(detail)
                fingerprint = json.dumps(clean, sort_keys=True)
                now = time.monotonic()
                if fingerprint != previous or now - last_emitted >= 30 or state != 'pending':
                    print(json.dumps({'event': 'wait-progress', 'app': client.app['name'],
                          'status': state, 'elapsedSeconds': round(now - started, 1),
                          'progress': clean}, ensure_ascii=True), file=sys.stderr, flush=True)
                    previous, last_emitted = fingerprint, now
            if state != 'pending':
                break
            remaining = deadline - time.monotonic()
            if remaining > 0:
                next_poll = time.monotonic() + min(interval, remaining)
                while True:
                    delay = next_poll - time.monotonic()
                    if delay <= 0:
                        break
                    time.sleep(min(60, delay))
    except DeadlineReached:
        state = 'timeout'
    except KeyboardInterrupt:
        state = 'interrupted'
    resume = ['autoarr'] + (['--config', str(config)] if config else []) + [
        client.app['name'], 'wait', '--resume', json.dumps(target, separators=(',', ':')),
        '--wait-timeout', str(timeout), '--poll-interval', str(interval)]
    if quiet:
        resume.append('--quiet-progress')
    return {'wait': True, 'status': state, 'app': client.app['name'], 'target': target,
            'elapsedSeconds': round(time.monotonic() - started, 1), 'polls': polls,
            'progress': detail, 'resume': resume, 'response': response}


def exit_code(result):
    if isinstance(result, dict) and result.get('wait') is True:
        return {'completed': 0, 'timeout': 3, 'failed': 2, 'interrupted': 130}.get(result.get('status'), 2)
    return 0
