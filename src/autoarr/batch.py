"""Run repeatable CLI tasks without shell execution or repetitive model turns."""
import concurrent.futures
import contextlib
import io
import json
import re

from .config import AutoarrError, app_config
from .output import predicate, prepare_result, records
from .waiting import exit_code as wait_exit_code


def id_values(text):
    result = []
    for part in text.split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            bounds = part.split('-')
            if len(bounds) == 2 and all(x.isdigit() for x in bounds):
                first, last = map(int, bounds)
                if last < first or last - first > 999:
                    raise AutoarrError('ID ranges must be ascending and contain at most 1,000 entries')
                result += list(range(first, last + 1))
                continue
        result.append(part)
    if not result or len(result) > 1000:
        raise AutoarrError('A batch needs 1–1,000 explicit targets')
    return result


PLACEHOLDER = re.compile(r'\{([A-Za-z_][A-Za-z0-9_.]*)\}')


def substitute(argv, values, defer_steps=False):
    result = []
    for argument in argv:
        def lookup(field):
            if defer_steps and field.startswith('steps.'):
                return '{' + field + '}'
            if field not in values:
                raise AutoarrError('Unknown batch placeholder: ' + field)
            return values[field]
        def replace(match):
            field = match[1]
            value = lookup(field)
            return json.dumps(value, separators=(',', ':')) if isinstance(value, (dict, list, bool)) or value is None else str(value)
        def json_replace(value):
            if isinstance(value, dict):
                return {k: json_replace(v) for k, v in value.items()}
            if isinstance(value, list):
                return [json_replace(v) for v in value]
            if isinstance(value, str):
                match = PLACEHOLDER.fullmatch(value)
                return lookup(match[1]) if match else PLACEHOLDER.sub(replace, value)
            return value
        if argument.startswith(('{', '[')):
            try:
                parsed = json.loads(argument)
            except ValueError:
                pass
            else:
                result.append(json.dumps(json_replace(parsed), separators=(',', ':')))
                continue
        # JSON object braces are ordinary text. Only named placeholders are
        # substituted, and no shell or Python expression is evaluated.
        result.append(PLACEHOLDER.sub(replace, argument))
    return result


def expand(args, load_json):
    template = list(args.command)
    if template and template[0] == '--':
        template.pop(0)
    jobs = []
    if args.file:
        if template or args.apps or args.ids:
            raise AutoarrError('Use either --file or --apps/--ids with a command template')
        data = load_json(args.file)
        if not isinstance(data, list):
            raise AutoarrError('Batch JSON must be an array of argv arrays or {argv, foreach} objects')
        for item in data:
            if isinstance(item, list):
                jobs.append({'argv': item})
            elif isinstance(item, dict) and isinstance(item.get('argv'), list):
                if not item['argv'] or not all(isinstance(value, str) for value in item['argv']):
                    raise AutoarrError('Each batch command must be a nonempty array of strings')
                values = item.get('foreach', [None])
                if not isinstance(values, list):
                    raise AutoarrError('foreach must be an array')
                for value in values:
                    context = {'item': value}
                    if isinstance(value, dict):
                        context.update({'item.' + k: v for k, v in value.items()})
                    job = {'argv': substitute(item['argv'], context, defer_steps=True), 'expect': item.get('expect', [])}
                    if 'name' in item:
                        if not isinstance(item['name'], str):
                            raise AutoarrError('Batch step names must be strings')
                        job['name'] = substitute([item['name']], context)[0]
                    jobs.append(job)
            else:
                raise AutoarrError('Invalid batch task; argv must be an array, never a shell command')
    else:
        if not template or not (args.apps or args.ids):
            raise AutoarrError('Provide --file, or --apps/--ids followed by -- COMMAND')
        apps = [v.strip() for v in args.apps.split(',')] if args.apps else [None]
        ids = id_values(args.ids) if args.ids else [None]
        if any(not app for app in apps) and args.apps:
            raise AutoarrError('App names cannot be empty')
        for app in apps:
            for identifier in ids:
                command = substitute(template, {'app': app, 'id': identifier})
                jobs.append({'argv': ([app] if app else []) + command})
    if not jobs or len(jobs) > 1000:
        raise AutoarrError('A batch must contain between 1 and 1,000 jobs')
    if any(not isinstance(job['argv'], list) or not job['argv'] or not all(isinstance(v, str) for v in job['argv']) for job in jobs):
        raise AutoarrError('Each batch command must be a nonempty array of strings')
    return jobs


def read_only(args):
    if args.scope in ('apps', 'doctor', 'result'):
        return True
    action = getattr(args, 'action', None)
    if action == 'wait':
        return any(getattr(args, key, None) for key in ('command_id', 'download_id', 'torrent_hash', 'task_id'))
    if action in {'status', 'health', 'ops', 'schema', 'list', 'get', 'queue', 'history',
                  'roots', 'profiles', 'tags', 'indexers', 'clients', 'files', 'wanted',
                  'lookup', 'releases', 'items', 'images', 'libraries', 'tasks', 'torrents',
                  'torrent', 'transfer', 'search', 'missing', 'find-missing'}:
        return True
    if action in ('metadata', 'library-options'):
        return not args.data and not args.set
    if action in ('manual-import', 'rename'):
        return not args.apply
    # Raw endpoints and discovered calls are conservative: even GET can mutate
    # Subsonic state. Do not classify arbitrary requests as parallel-safe.
    return False


def execute(args, path, config, parse, run, load_json):
    jobs = expand(args, load_json)
    prepared, named = [], {}
    for job in jobs:
        argv = job['argv']
        name = job.get('name')
        if name is not None and (not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', name) or name in named):
            raise AutoarrError('Batch step names must be unique identifiers (letters, digits, underscores)')
        references = {}
        for argument in argv:
            for field in PLACEHOLDER.findall(argument):
                if not field.startswith('steps.'):
                    continue
                parts = field.split('.')
                if (len(parts) < 3 or parts[2] not in ('data', 'result') or parts[1] not in named
                        or parts[2] == 'result' and len(parts) != 3):
                    raise AutoarrError('Step references need an earlier named step: {steps.NAME.data.FIELD} or {steps.NAME.result}')
                references[field] = parts[1]
        expectations = job.get('expect', [])
        if not isinstance(expectations, list) or not all(isinstance(expr, str) for expr in expectations):
            raise AutoarrError('Batch expect must be an array of filter expressions')
        checks = [predicate(expr) for expr in expectations]
        if any(v == '--config' or v.startswith('--config=') or v == '--api-key-stdin' for v in argv):
            raise AutoarrError('Batch children cannot replace configuration or read credentials from stdin')
        command = ['--config', str(path), '--timeout', str(args.timeout)]
        if args.dry_run:
            command += ['--dry-run']
        if args.no_cache:
            command += ['--no-cache']
        command += argv
        # Validate all static syntax/connections before any job starts. Dynamic
        # values are checked again after binding, before their own job starts.
        preview = substitute(command, {field: 1 for field in references}) if references else command
        parsed = parse_job(preview, config, parse, 'Invalid batch child arguments; no jobs were started')
        if parsed.scope in ('batch', 'config', 'init'):
            raise AutoarrError('Batch nesting, credential configuration, and SSH initialization are not batch tasks')
        if parsed.scope not in ('apps', 'doctor', 'result'):
            app_config(config, parsed.scope)
        if getattr(parsed, 'data', None) == '-':
            raise AutoarrError('Batch bodies must use JSON literals or @files, not shared stdin')
        prepared.append({**job, 'argv': command, 'parsed': parsed, 'references': references, 'checks': checks})
        if name is not None:
            named[name] = prepared[-1]
    for job in prepared:
        for field, source in job['references'].items():
            source_args = named[source]['parsed']
            if field.split('.')[2] == 'result' and (source_args.no_cache or source_args.scope in ('apps', 'doctor')):
                raise AutoarrError('A result reference needs a cached source; use .data for uncached steps')
    needed = {source for job in prepared for source in job['references'].values()}
    completed = {}

    def one(index):
        job = prepared[index]
        command, parsed = job['argv'], job['parsed']
        identity = {'job': index + 1, **({'name': job['name']} if job.get('name') else {}),
                    'app': parsed.scope, 'action': getattr(parsed, 'action', parsed.scope)}
        try:
            if job['references']:
                if any(not completed[source]['ok'] for source in job['references'].values()):
                    return {**identity, 'skipped': True, 'reason': 'A referenced step did not succeed'}, {'ok': False}
                values = {field: step_value(completed, field) for field in job['references']}
                resolved = substitute(command, values)
                for original, value in zip(command, resolved):
                    if any(field in original for field in job['references']):
                        # Inspect the value inside --option=VALUE as well as
                        # standalone arguments; --data=@file must not sneak in
                        # through an attached option's literal '--' prefix.
                        original_value = original.split('=', 1)[1] if original.startswith('--') and '=' in original else original
                        resolved_value = value.split('=', 1)[1] if original.startswith('--') and '=' in original else value
                        if resolved_value.startswith(('-', '@')) and not original_value.startswith(resolved_value[0]):
                            raise AutoarrError('A step reference cannot introduce CLI flags or file/stdin input')
                command = resolved
                parsed = parse_job(command, config, parse, 'Resolved batch arguments are invalid; this job was not started')
            redactor, child_args, data = run(command, parsed=parsed, config=config, path=path)
            if child_args.scope == 'doctor' and any(not r['ok'] for r in data):
                raise AutoarrError('A doctor check failed')
            child_args.limit = 3 if child_args.limit is None else min(child_args.limit, 3)
            child_args.full = False
            child_args.max_output = 2000
            summary = prepare_result(redactor.clean(data), child_args, getattr(child_args, 'result_id', None),
                                     getattr(child_args, 'output_action', getattr(child_args, 'action', None)))
            code = wait_exit_code(data)
            frame = {'data': data, 'count': len(records(data)) if records(data) is not None else 1}
            unmet = [expr for expr, check in zip(job.get('expect', []), job['checks']) if not check(frame)]
            ok = code == 0 and not unmet
            return {**identity, 'ok': ok, **({'exitCode': code} if code else {}),
                    **({'error': 'Expectation failed: ' + ', '.join(unmet)} if unmet else {}), 'data': summary}, {
                        'ok': ok, **({'data': data, 'result': getattr(child_args, 'result_id', None)} if job.get('name') in needed else {})}
        except (AutoarrError, ValueError, OSError) as e:
            return {**identity, 'ok': False, 'error': str(e)}, {'ok': False}

    results = []
    failed, used_parallel, index = False, False, 0
    while index < len(prepared):
        if failed and not args.keep_going:
            results.append({'job': index + 1, **({'name': prepared[index]['name']} if prepared[index].get('name') else {}), 'skipped': True})
            if prepared[index].get('name'):
                completed[prepared[index]['name']] = {'ok': False}
            index += 1
            continue
        end = index + 1
        if args.workers > 1 and read_only(prepared[index]['parsed']) and not prepared[index]['references']:
            while end < len(prepared) and read_only(prepared[end]['parsed']) and not prepared[end]['references']:
                end += 1
        if end - index > 1:
            used_parallel = True
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
                outcomes = list(pool.map(one, range(index, end)))
        else:
            outcomes = [one(index)]
        for offset, (item, private) in enumerate(outcomes):
            results.append(item)
            name = prepared[index + offset].get('name')
            if name:
                completed[name] = private
            failed = failed or item.get('ok') is False
        index = end
    all_reads = all(read_only(job['parsed']) for job in prepared)
    return {'total': len(prepared), 'succeeded': sum(r.get('ok', False) for r in results),
            'failed': sum(r.get('ok') is False for r in results), 'skipped': sum(r.get('skipped', False) for r in results),
            'execution': ('parallel reads' if all_reads else 'parallel reads with sequential writes') if used_parallel else 'sequential',
            'dryRun': args.dry_run, 'jobs': results}


def parse_job(argv, config, parse, error):
    # argparse's raw errors can contain untrusted response values. Keep them out
    # of logs; the originating step has a private result for inspection instead.
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            return parse(argv, config)
        except SystemExit:
            raise AutoarrError(error) from None


def step_value(completed, field):
    parts = field.split('.')
    value = completed[parts[1]]
    for part in parts[2:]:
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            raise AutoarrError('Step reference has no value: ' + field)
    return value
