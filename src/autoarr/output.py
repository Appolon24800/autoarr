import json
import re
import copy
import math

from .config import AutoarrError


def parse_value(text):
    try:
        return json.loads(text)
    except ValueError:
        return text


def pairs(values):
    result = []
    for value in values or []:
        if '=' not in value:
            raise AutoarrError('Expected KEY=VALUE: ' + value)
        k, v = value.split('=', 1)
        if not k:
            raise AutoarrError('Empty field name')
        result.append((k, parse_value(v)))
    return result


def get_path(value, path):
    path = re.sub(r'\[([0-9]*)\]', lambda m: '.' + (m[1] or '*'), path)
    def walk(obj, parts):
        if not parts:
            return obj
        part, *rest = parts
        if part == '*':
            values = list(obj.values()) if isinstance(obj, dict) else obj if isinstance(obj, list) else []
            found = [walk(v, rest) for v in values]
            return [leaf for v in found for leaf in (v if isinstance(v, list) else [v]) if leaf is not None]
        if isinstance(obj, dict):
            return walk(obj.get(part), rest)
        if isinstance(obj, list) and part.isdigit() and int(part) < len(obj):
            return walk(obj[int(part)], rest)
        return None
    return walk(value, path.split('.'))


def set_path(value, path, new):
    parts = path.split('.')
    obj = value
    for part in parts[:-1]:
        if isinstance(obj, list) and part.isdigit() and int(part) < len(obj):
            obj = obj[int(part)]
        elif isinstance(obj, dict):
            obj = obj.setdefault(part, {})
        else:
            raise AutoarrError('Cannot set nested field: ' + path)
    if isinstance(obj, list) and parts[-1].isdigit() and int(parts[-1]) < len(obj):
        obj[int(parts[-1])] = new
    elif isinstance(obj, dict):
        obj[parts[-1]] = new
    else:
        raise AutoarrError('Cannot set nested field: ' + path)


def records(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for k in ('records', 'Items', 'data', 'results', 'rows'):
            if isinstance(value.get(k), list):
                return value[k]
    return None


def predicate(expression):
    match = re.fullmatch(r'\s*(.+?)\s+(notin|in|contains|overlaps|between|exists|missing)(?:\s+(.*))?\s*', expression)
    if not match:
        match = re.fullmatch(r'\s*(.+?)\s*(!~=|~=|=~|!=|>=|<=|=|>|<)\s*(.*?)\s*', expression)
    if not match:
        raise AutoarrError('Invalid filter. Use FIELD~=TEXT, FIELD in [1,2], FIELD contains VALUE, FIELD exists, etc.')
    field, op, text = match.groups()
    field = field.strip()
    expected = parse_value(text or '')
    if op in ('in', 'notin', 'overlaps', 'between') and not isinstance(expected, list):
        raise AutoarrError(op + ' needs a JSON array')
    if op == 'between' and len(expected) != 2:
        raise AutoarrError('between needs exactly two bounds')
    pattern = None
    if op == '=~':
        if len(str(expected)) > 512:
            raise AutoarrError('Filter regex is too long (maximum 512 characters)')
        try:
            pattern = re.compile(str(expected), re.I)
        except re.error as e:
            raise AutoarrError('Invalid filter regex: ' + str(e)) from None
    def compare(value):
        if op == '=': return value == expected
        if op == '!=': return value != expected
        if op == '~=': return str(expected).casefold() in str(value if value is not None else '').casefold()
        if op == '!~=': return str(expected).casefold() not in str(value if value is not None else '').casefold()
        if op == '=~': return bool(pattern.search(str(value if value is not None else '')))
        if op == 'in': return value in expected
        if op == 'notin': return value not in expected
        try:
            if op == '>': return value > expected
            if op == '<': return value < expected
            if op == '>=': return value >= expected
            if op == '<=': return value <= expected
            if op == 'between': return expected[0] <= value <= expected[1]
        except TypeError:
            return False
        return False
    def matches(row):
        value = get_path(row, field)
        if op == 'exists': return value is not None and value != []
        if op == 'missing': return value is None or value == []
        if op == 'contains':
            return expected in value if isinstance(value, (list, dict)) else str(expected).casefold() in str(value or '').casefold()
        if op == 'overlaps':
            return isinstance(value, list) and any(v in expected for v in value)
        if isinstance(value, list):
            return all(compare(v) for v in value) if op in ('!=', '!~=', 'notin') else any(compare(v) for v in value)
        return compare(value)
    return matches


def filter_rows(rows, filters=None, any_filters=None):
    required = [predicate(x) for x in filters or []]
    alternatives = [predicate(x) for x in any_filters or []]
    return [r for r in rows if all(p(r) for p in required) and (not alternatives or any(p(r) for p in alternatives))]


def sort_rows(rows, fields):
    rows = list(rows)
    for field in reversed((fields or '').split(',')):
        if not field:
            continue
        descending = field.startswith('-')
        name = field.lstrip('+-')
        def key(row):
            value = get_path(row, name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return (0, value if math.isfinite(value) else 0)
            return (1, json.dumps(value, ensure_ascii=False, sort_keys=True).casefold())
        present = [r for r in rows if get_path(r, name) is not None]
        absent = [r for r in rows if get_path(r, name) is None]
        rows = sorted(present, key=key, reverse=descending) + absent
    return rows


def unique_rows(rows, fields):
    if not fields:
        return rows
    seen, result = set(), []
    for row in rows:
        values = [get_path(row, f) for f in fields.split(',')]
        key = json.dumps(values, sort_keys=True)
        # Missing identifiers never collapse unrelated records into one item.
        if all(v is None or v == '' for v in values) or key not in seen:
            result.append(row)
            seen.add(key)
    return result


def format_result(value, select=None, filters=None, limit=None, table=False):
    rows = records(value)
    if rows is not None:
        rows = filter_rows(rows, filters)
        if limit is not None:
            rows = rows[:limit]
        if select:
            rows = [{k: get_path(row, k) for k in select.split(',')} for row in rows]
        if isinstance(value, dict):
            key = next(k for k in ('records', 'Items', 'data', 'results', 'rows') if isinstance(value.get(k), list))
            value = {**value, key: rows}
        else:
            value = rows
    elif filters:
        raise AutoarrError('--filter needs a list response')
    elif select and isinstance(value, dict):
        value = {k: get_path(value, k) for k in select.split(',')}
    if not table:
        return json.dumps(value, ensure_ascii=False, indent=2)
    rows = records(value) if records(value) is not None else [value]
    if not rows:
        return '(no records)'
    if not all(isinstance(r, dict) for r in rows):
        return '\n'.join(str(r) for r in rows)
    keys = select.split(',') if select else list(dict.fromkeys(k for r in rows for k in r))[:10]
    # Escape control characters so release titles cannot inject terminal controls.
    def cell(v):
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False, separators=(',', ':'))
        text = str('' if v is None else v)
        text = re.sub(r'[\x00-\x1f\x7f]', lambda m: repr(m.group())[1:-1], text)
        return text if len(text) <= 90 else text[:87] + '...'
    data = [[cell(r.get(k)) for k in keys] for r in rows]
    widths = [max(len(k), *(len(r[i]) for r in data)) for i, k in enumerate(keys)]
    lines = ['  '.join(k.ljust(w) for k, w in zip(keys, widths)),
             '  '.join('-' * w for w in widths)]
    lines += ['  '.join(v.ljust(w) for v, w in zip(row, widths)) for row in data]
    return '\n'.join(lines)


def compact_value(value, depth=0):
    if isinstance(value, str):
        return value if len(value) <= 180 else value[:177] + '…'
    if isinstance(value, dict):
        if depth >= 3:
            return {'_fields': list(value)[:12], '_count': len(value)}
        priority = ['id', 'Id', 'name', 'Name', 'title', 'path', 'rootFolderPath', 'OriginalTitle',
                    'OriginalLanguage', 'monitored', 'qualityProfileId', 'movieIds', 'seriesId', 'files']
        ordered = [k for k in priority if k in value] + [k for k in value if k not in priority]
        items = [(k, value[k]) for k in ordered]
        result = {k: compact_value(v, depth + 1) for k, v in items[:12]}
        if len(items) > 12:
            result['_omittedFields'] = len(items) - 12
        return result
    if isinstance(value, list):
        return [compact_value(v, depth + 1) for v in value[:8]] + ([{'_more': len(value) - 8}] if len(value) > 8 else [])
    return value


def enrich(row, ref=None):
    if not isinstance(row, dict):
        return row
    row = dict(row)
    if ref:
        row['ref'] = ref
    if isinstance(row.get('size'), (int, float)):
        row['sizeMiB'] = round(row['size'] / 1024 ** 2, 1)
    if 'customFormatScore' in row:
        row['score'] = row['customFormatScore']
    if isinstance(row.get('statusMessages'), list):
        issues = []
        for status in row['statusMessages']:
            for message in status.get('messages') or [status.get('title')]:
                if message and message not in issues:
                    issues.append(message)
        row['issues'] = issues[:3]
        row['issueCount'] = len(issues)
    if 'seasonNumber' in row and 'episodeNumber' in row:
        row['code'] = f'S{row["seasonNumber"]:02d}E{row["episodeNumber"]:02d}'
    return row


def default_fields(row, action=''):
    if not isinstance(row, dict):
        return None
    if 'operationId' in row:
        if 'body' in row:
            return ['operationId', 'method', 'path', 'parameters', 'body', 'source']
        wanted = ['operationId', 'method', 'path']
    elif 'guid' in row and 'indexer' in row:
        wanted = ['ref', 'title', 'quality.quality.name', 'seeders', 'sizeMiB', 'indexer', 'score', 'rejections']
    elif 'episodeNumber' in row:
        wanted = ['id', 'code', 'title', 'hasFile', 'monitored', 'airDateUtc']
    elif 'trackedDownloadStatus' in row:
        wanted = ['ref', 'id', 'title', 'status', 'trackedDownloadStatus', 'trackedDownloadState', 'issues', 'issueCount']
    elif 'app' in row and 'ok' in row:
        wanted = ['app', 'ok', 'version', 'error']
    elif 'relativePath' in row:
        wanted = ['id', 'relativePath', 'languages.*.name', 'mediaInfo.audioLanguages', 'mediaInfo.subtitles']
    else:
        wanted = ['ref', 'id', 'Id', 'title', 'Name', 'name', 'OriginalTitle', 'OriginalLanguage',
                  'path', 'Path', 'status', 'State', 'ok', 'version', 'monitored', 'enable',
                  'seeders', 'progress', 'statistics.episodeFileCount', 'statistics.episodeCount',
                  'trackedDownloadStatus', 'statusMessages', 'error', 'ImageType', 'Width', 'Height']
    found = [k for k in wanted if get_path(row, k) is not None]
    return found or list(row)[:8]


def schema_brief(value):
    if 'paths' in value:
        verbs = {'get', 'post', 'put', 'patch', 'delete', 'head', 'options'}
        operations = sum(k in verbs for path in value['paths'].values() for k in path)
        return {'info': value.get('info'), 'paths': len(value['paths']), 'operations': operations,
                'source': value.get('x-autoarr-source'), 'hint': 'Use ops --search TERM --details; schema OPERATION_ID --full returns full details.'}
    body = value.get('requestBody', {})
    content = body.get('content', {})
    schema = next(iter(content.values()), {}).get('schema', {})
    while 'allOf' in schema and len(schema['allOf']) == 1:
        schema = schema['allOf'][0]
    def typename(v):
        return v.get('type') or v.get('$ref', '').rsplit('/', 1)[-1] or '|'.join(typename(x) for x in v.get('allOf', v.get('anyOf', []))) or '?'
    properties = schema.get('properties', schema.get('items', {}).get('properties', {}))
    def choices(v):
        return v.get('enum', v.get('items', {}).get('enum'))
    return {'operationId': value.get('operationId'), 'method': value.get('method'), 'path': value.get('path'),
            'parameters': [{k: v for k, v in {'name': p['name'], 'in': p['in'],
                'type': typename(p.get('schema', p)), 'required': p.get('required', False),
                **({'choices': choices(p.get('schema', p))} if choices(p.get('schema', p)) is not None else {})}.items()}
                for p in value.get('parameters', [])],
            'body': {'required': body.get('required', False), 'type': typename(schema),
                     'fields': {k: typename(v) for k, v in properties.items()},
                     **({'requiredFields': schema['required']} if schema.get('required') else {}),
                     **({'choices': {k: choices(v) for k, v in properties.items() if choices(v) is not None}}
                        if any(choices(v) is not None for v in properties.values()) else {})},
            'source': value.get('schemaSource')}


def prepare_result(value, args, result_id=None, action=None):
    """Output-only transformations. The native data and cached payload stay intact."""
    action = action or getattr(args, 'action', '')
    full = getattr(args, 'full', False)
    selection = getattr(args, 'select', None)
    rows = records(value)
    filters = getattr(args, 'filter', [])
    any_filters = getattr(args, 'any_filter', [])
    limit = getattr(args, 'limit', None)
    if rows is not None:
        source_rows = rows
        rows = [enrich(r, f'{result_id}:{i}' if result_id else None) for i, r in enumerate(rows)]
        original_rows = {id(row): original for row, original in zip(rows, source_rows)}
        rows = filter_rows(rows, filters, any_filters)
        sort = getattr(args, 'sort', None)
        if not sort and action == 'releases' and not full:
            sort = '-customFormatScore,-seeders,size'
        rows = unique_rows(sort_rows(rows, sort), getattr(args, 'unique_by', None))
        matched = len(rows)
        if getattr(args, 'count', False):
            return {'matched': matched, **({'result': result_id} if result_id else {})}
        if getattr(args, 'group_by', None):
            groups = {}
            fields = args.group_by.split(',')
            for row in rows:
                key = json.dumps([get_path(row, f) for f in fields], sort_keys=True)
                groups[key] = groups.get(key, 0) + 1
            result = [{'key': dict(zip(fields, json.loads(k))), 'count': n} for k, n in groups.items()]
            result.sort(key=lambda x: -x['count'])
            return {'matched': matched, 'groups': result[:limit if limit is not None else len(result)],
                    **({'result': result_id} if result_id else {})}
        if limit is None and not full:
            limit = 10
        shown = rows if limit is None else rows[:limit]
        if selection:
            shown = [{k: get_path(row, k) for k in selection.split(',')} if isinstance(row, dict) else row for row in shown]
        elif not full:
            shown = [{k: get_path(row, k) for k in default_fields(row, action)} if isinstance(row, dict) else row for row in shown]
        if full:
            # Derived fields are available when selected; otherwise full keeps
            # native records rather than injecting refs into API objects.
            if not selection:
                shown = [original_rows[id(r)] for r in shown]
            if isinstance(value, list):
                return shown
            key = next(k for k in ('records', 'Items', 'data', 'results', 'rows') if isinstance(value.get(k), list))
            return {**value, key: shown}
        report = {'fetched': len(source_rows), 'matched': matched, 'shown': len(shown),
                  'rows': [compact_value(row) for row in shown]}
        if isinstance(value, dict):
            total = value.get('totalRecords', value.get('TotalRecordCount', value.get('total')))
            if total is not None: report['available'] = total
        if result_id: report['result'] = result_id
        if len(shown) < matched: report['more'] = matched - len(shown)
        return report
    if filters or any_filters or getattr(args, 'count', False) or getattr(args, 'group_by', None):
        raise AutoarrError('Filters/count/group-by need a list response')
    if selection and isinstance(value, dict):
        value = {k: get_path(value, k) for k in selection.split(',')}
    elif not full and action == 'schema' and isinstance(value, dict):
        value = schema_brief(value)
    elif not full and isinstance(value, dict) and ('Id' in value or 'OriginalTitle' in value):
        fields = default_fields(value, action) + (['Overview'] if 'Overview' in value else [])
        value = {k: get_path(value, k) for k in dict.fromkeys(fields)}
    if full:
        return value
    if isinstance(value, dict) and value.get('wait') is True:
        # Targets and argv must survive intact: compact_value truncates long
        # strings and arrays, which would make an otherwise resumable wait unsafe.
        return {**value, 'progress': compact_value(value.get('progress', {})),
                'response': compact_value(value.get('response')),
                **({'result': result_id} if result_id else {})}
    if action in ('find-missing', 'batch'):
        return {**value, **({'result': result_id} if result_id else {})}
    result = compact_value(value)
    if result_id and isinstance(result, dict):
        result['result'] = result_id
    return result


def render_result(value, args, result_id=None, action=None):
    report = prepare_result(value, args, result_id, action)
    full = getattr(args, 'full', False)
    budget = getattr(args, 'max_output', None) or (None if full else 6000)
    if not full and isinstance(report, dict):
        if getattr(args, 'cached_at', None): report['cachedAt'] = args.cached_at
        if getattr(args, 'cache_error', None): report['warning'] = args.cache_error
    def dumps(data):
        return json.dumps(data, ensure_ascii=False, indent=2 if getattr(args, 'pretty', False) else None,
                          separators=None if getattr(args, 'pretty', False) else (',', ':'))
    if budget and len(dumps(report)) > budget:
        if full:
            raise AutoarrError('Full output exceeds --max-output; use --select/--limit or omit --full')
        report = copy.deepcopy(report)
        if isinstance(report, dict):
            sequence_key = next((k for k in ('rows', 'groups', 'jobs', 'episodes') if isinstance(report.get(k), list)), None)
            if sequence_key:
                original_count = len(report[sequence_key])
                report['outputLimited'] = True
                if sequence_key != 'rows':
                    report['omitted'] = 0
                while report[sequence_key] and len(dumps(report)) > budget:
                    report[sequence_key].pop()
                    if sequence_key != 'rows':
                        report['omitted'] = original_count - len(report[sequence_key])
                if sequence_key == 'rows':
                    report['shown'] = len(report['rows'])
                    report['more'] = report['matched'] - report['shown']
                else:
                    report['omitted'] = original_count - len(report[sequence_key])
        if len(dumps(report)) > budget:
            if isinstance(report, dict) and report.get('wait') is True:
                report.pop('response', None)
                report.pop('progress', None)
                if len(dumps(report)) > budget:
                    report = {k: report[k] for k in ('wait', 'status', 'app', 'target', 'result') if k in report}
                    report['outputLimited'] = True
                if len(dumps(report)) > budget and result_id:
                    report = {'wait': True, 'status': report['status'], 'app': report['app'],
                              'result': result_id, 'outputLimited': True,
                              'hint': 'Read result --id RESULT --full for the resume target. Do not repeat the original action.'}
            if len(dumps(report)) > budget:
                if isinstance(report, dict) and report.get('wait') is True:
                    raise AutoarrError('Wait output exceeds --max-output; do not repeat the original action. '
                                       'Use a larger budget and enable caching for oversized wait targets.')
                report = {'outputLimited': True, 'hint': 'Use --select to retrieve specific fields, or --full.',
                          **({'result': result_id} if result_id else {})}
    if getattr(args, 'table', False):
        text = format_result(report, table=True)
        if isinstance(report, dict) and 'rows' in report:
            text += '\n' + dumps({k: v for k, v in report.items() if k != 'rows'})
        if budget and len(text) > budget:
            return dumps(report)
        return text
    return dumps(report)
