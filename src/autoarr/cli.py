import argparse
import base64
import concurrent.futures
import json
import math
import mimetypes
import os
import sys
from pathlib import Path

from . import __version__
from .bootstrap import bootstrap
from .catalog import Catalog
from .client import Client
from .config import ARR, KINDS, MANAGERS, AutoarrError, app_config, config_path, private_json, read_config
from .output import pairs, render_result, set_path
from .results import Results
from .query import server_filters
from .security import Redactor
from .waiting import exit_code as wait_exit_code, validate_options, validate_target, wait_for
from .workflows import (add_media, command, image_set, jellyfin_user, metadata, move,
                        paginated, require, resource_path, updated_body)


def wait_flags(p):
    p.add_argument('--wait', action='store_true', help='wait for completion before returning; progress goes to stderr')
    p.add_argument('--wait-timeout', type=float, default=300, help='wait budget in seconds; timeout leaves the native job running (default 300)')
    p.add_argument('--poll-interval', type=float, default=5, help='seconds between wait checks (default 5)')
    p.add_argument('--quiet-progress', action='store_true', help='suppress wait progress on stderr')
    p.add_argument('--wait-path', help='read-only native endpoint to check after any action with --wait')
    p.add_argument('--until', action='append', default=[], metavar='EXPR', help='completion condition; repeat for AND, using filter syntax')
    p.add_argument('--fail-if', action='append', default=[], metavar='EXPR', help='failure condition; any match stops the wait')
    p.add_argument('--wait-until', choices=['downloaded', 'imported'], default='imported', help='Arr download completion goal (default imported)')


def common_parser():
    p = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    p.add_argument('--config', metavar='FILE', help='private connection JSON (or AUTOARR_CONFIG)')
    p.add_argument('--dry-run', action='store_true', help='read checks, then print write requests without sending them')
    p.add_argument('--timeout', type=float, default=90, help='HTTP timeout in seconds (default 90)')
    p.add_argument('--table', action='store_true', help='compact table instead of JSON')
    p.add_argument('--full', action='store_true', help='all native fields and rows; bypass compact defaults')
    p.add_argument('--pretty', action='store_true', help='indented JSON instead of single-line JSON')
    p.add_argument('--max-output', type=int, help='maximum output characters; compact default 6000')
    p.add_argument('--no-cache', action='store_true', help='do not save a private result snapshot')
    p.add_argument('--select', metavar='FIELDS', help='comma-separated output fields; nested paths supported')
    p.add_argument('--filter', action='append', default=[], metavar='EXPR', help='local list filter, e.g. title~=Monogatari')
    p.add_argument('--any-filter', action='append', default=[], metavar='EXPR', help='OR filters, combined with the AND --filter conditions')
    p.add_argument('--sort', help='sort fields, e.g. -seeders,size or title')
    p.add_argument('--unique-by', metavar='FIELDS', help='deduplicate by one or more fields after sorting')
    p.add_argument('--count', action='store_true', help='return the filtered count only')
    p.add_argument('--group-by', metavar='FIELDS', help='return grouped counts instead of records')
    p.add_argument('--limit', type=int, help='limit displayed records (after filtering)')
    p.add_argument('--all', action='store_true', help='follow supported pagination until exhausted')
    p.add_argument('--refresh-schema', action='store_true', help='reload API discovery instead of using its private cache')
    wait_flags(p)
    return p


def data_flags(p, query=True, fields=False, binary=False):
    if query:
        p.add_argument('-q', '--query', action='append', default=[], metavar='KEY=VALUE', help='query parameter; repeat for multiple values')
    p.add_argument('--data', metavar='JSON|@FILE|-', help='JSON literal, @file, or stdin (-)')
    if fields:
        p.add_argument('--set', action='append', default=[], metavar='FIELD=VALUE', help='JSON field; supports nested fields and existing array indices')
    if binary:
        p.add_argument('--form', action='append', default=[], metavar='KEY=VALUE', help='URL-encoded form field')
        p.add_argument('--file', action='append', default=[], metavar='FIELD=FILE', help='multipart file upload; combines with --form')
        p.add_argument('--upload', metavar='FILE', help='binary request body from a file')
        p.add_argument('--base64-upload', action='store_true', help='base64-encode an upload, as required by Jellyfin image uploads')
        p.add_argument('--content-type', help='MIME type for an upload')
        p.add_argument('--output', metavar='FILE', help='save the exact response to a new private file (including binary responses)')


def query_flags(p):
    p.add_argument('-q', '--query', action='append', default=[], metavar='KEY=VALUE')


def parse(argv, config):
    common = common_parser()
    global_args, remaining = common.parse_known_args(argv)
    p = argparse.ArgumentParser(prog='autoarr', parents=[common], allow_abbrev=False,
        description='Direct application APIs for your media stack. Normal commands never use SSH.',
        epilog='Examples: autoarr sonarr queue --all; autoarr radarr ops --search import; autoarr jellyfin metadata ID --set OriginalTitle="Monogatari Series"')
    p.add_argument('--version', action='version', version=__version__)
    sub = p.add_subparsers(dest='scope', required=True)
    sub.add_parser('apps', help='configured applications and connection details, without credentials')
    sub.add_parser('doctor', help='test all configured connections using read-only status calls')
    batch = sub.add_parser('batch', help='run parallel reads, sequential writes, or named dependent steps in one invocation',
        epilog='JSON steps accept name, argv, expect, and foreach. Bind earlier native responses with {steps.NAME.data.FIELD} '
               'or cached snapshots with {steps.NAME.result}. Example guard: "expect": ["count=1"].')
    batch.add_argument('--apps', help='comma-separated configured app names; prefix each command with the app')
    batch.add_argument('--ids', help='comma-separated IDs/ranges substituted into {id}')
    batch.add_argument('--file', help='JSON tasks from @file, inline JSON, or - for stdin')
    batch.add_argument('--workers', type=int, default=4, help='parallel workers for independent read commands only (1–8)')
    batch.add_argument('--keep-going', action='store_true', help='continue sequential jobs after a failed job')
    batch.add_argument('command', nargs=argparse.REMAINDER, help='-- COMMAND ARGS, with optional {id}/{app} placeholders')
    result = sub.add_parser('result', help='filter a cached response or saved JSON locally, without another API call')
    source = result.add_mutually_exclusive_group(required=True)
    source.add_argument('--id', dest='cached_id', help='result ID from a previous compact response')
    source.add_argument('--file', dest='result_file', help='saved native JSON file')
    init = sub.add_parser('init', help='one-time read-only SSH bootstrap for this server stack')
    init.add_argument('--ssh', required=True, help='existing SSH destination, e.g. user@server.example.invalid')
    init.add_argument('--host', help='host/IP used for subsequent direct HTTP calls; defaults to SSH hostname')
    init.add_argument('--overwrite', action='store_true')
    cfg = sub.add_parser('config', help='manage private connections')
    cfg_sub = cfg.add_subparsers(dest='config_action', required=True)
    cfg_sub.add_parser('show')
    add = cfg_sub.add_parser('add')
    add.add_argument('name')
    add.add_argument('--kind', required=True, choices=sorted(KINDS))
    add.add_argument('--url', required=True)
    add.add_argument('--api-prefix')
    auth = add.add_mutually_exclusive_group()
    auth.add_argument('--api-key-env', help='name of an environment variable holding the key')
    auth.add_argument('--api-key-stdin', action='store_true', help='read the key privately from stdin')
    add.add_argument('--auth', choices=['header', 'bearer', 'none', 'query', 'subsonic'])
    add.add_argument('--api-key-header')
    add.add_argument('--username')
    add.add_argument('--password-env')
    add.add_argument('--schema-path')
    add.add_argument('--status-path')
    add.add_argument('--user-id', help='default Jellyfin user for item metadata reads')
    for name in sorted(set(KINDS) - {'generic'} | set(config.get('apps', {}))):
        ap = sub.add_parser(name, help=f'{name} operations')
        # Keep root help/choices complete, but only construct the selected app.
        # Batch preflight used to build hundreds of unrelated parsers per job.
        if not remaining or remaining[0] != name:
            continue
        a = ap.add_subparsers(dest='action', required=True)
        a.add_parser('status', help='application version and status')
        a.add_parser('health', help='health warnings (Arr/Bazarr)')
        ops = a.add_parser('ops', help='discover every operation exposed in the live OpenAPI schema')
        ops.add_argument('--search', default='')
        ops.add_argument('--method', choices=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])
        ops.add_argument('--details', action='store_true', help='include compact parameters and body fields; no separate schema call needed')
        schema = a.add_parser('schema', help='full API schema or detailed operation parameters and request schema')
        schema.add_argument('operation', nargs='?')
        call = a.add_parser('call', help='invoke a discovered operation by its operationId')
        call.add_argument('operation')
        call.add_argument('-p', '--path', action='append', default=[], metavar='NAME=VALUE')
        data_flags(call, binary=True)
        api = a.add_parser('api', help='any native API endpoint: METHOD PATH; no endpoint whitelist')
        api.add_argument('method', choices=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'])
        api.add_argument('path')
        api.add_argument('--site', action='store_true', help='path is outside the API prefix but inside this app URL')
        data_flags(api, binary=True)
        waiter = a.add_parser('wait', help='wait for existing work without submitting it again')
        wait_target = waiter.add_mutually_exclusive_group(required=True)
        wait_target.add_argument('--command-id', help='existing native Arr command ID')
        wait_target.add_argument('--download-id', help='Arr download ID (not the queue record ID)')
        wait_target.add_argument('--torrent-hash', help='one existing qBittorrent torrent hash')
        wait_target.add_argument('--task-id', help='Jellyfin scheduled task ID')
        wait_target.add_argument('--path', help='read-only native endpoint; requires --until')
        wait_target.add_argument('--resume', help='exact JSON target from a previous wait response')
        query_flags(waiter)
        for verb in ('list', 'get', 'create', 'update', 'delete'):
            resource = a.add_parser(verb, help=f'{verb} a native API resource')
            resource.add_argument('resource')
            if verb in ('get', 'update', 'delete'):
                resource.add_argument('id', nargs='?')
            if verb in ('create', 'update'):
                data_flags(resource, fields=True)
            else:
                query_flags(resource)
            if verb == 'update':
                resource.add_argument('--method', choices=['PUT', 'PATCH', 'POST'], default='PUT')
            if verb == 'delete':
                resource.add_argument('--delete-files', action='store_true', help='explicitly delete managed files with a movie/series/artist/author')
        for action in ('queue', 'history', 'roots', 'profiles', 'tags', 'indexers', 'clients', 'files'):
            item = a.add_parser(action, help=action + ' shortcut; accepts native query parameters')
            query_flags(item)
        wanted = a.add_parser('wanted', help='missing or cutoff-unmet media; Bazarr missing subtitles')
        wanted.add_argument('--cutoff', action='store_true')
        wanted.add_argument('--media', choices=['movies', 'episodes'], default='episodes')
        query_flags(wanted)
        lookup = a.add_parser('lookup', help='find movies/shows/artists/authors to add')
        lookup.add_argument('term')
        query_flags(lookup)
        new = a.add_parser('add', help='add an exact lookup match using existing root/profile settings')
        new.add_argument('term', help='exact lookup term, e.g. tmdb:550 or tvdb:102261')
        new.add_argument('--root', required=True)
        new.add_argument('--profile-id', required=True, type=int)
        new.add_argument('--metadata-profile-id', type=int)
        new.add_argument('--language-profile-id', type=int)
        new.add_argument('--search', action='store_true', help='also search for missing media after adding')
        new.add_argument('--series-type', choices=['standard', 'anime', 'daily'], default='standard')
        new.add_argument('--monitor', choices=['all', 'future', 'missing', 'existing', 'firstSeason', 'latestSeason', 'none'], default='all')
        release = a.add_parser('releases', help='interactive release search through the app')
        release.add_argument('--movie-id', type=int)
        release.add_argument('--series-id', type=int)
        release.add_argument('--episode-id', type=int)
        release.add_argument('--season-number', type=int)
        release.add_argument('--artist-id', type=int)
        release.add_argument('--album-id', type=int)
        release.add_argument('--term', help='Prowlarr search term')
        query_flags(release)
        missing = a.add_parser('missing', help='aired missing Sonarr episodes for one series; excludes specials by default')
        find_missing = a.add_parser('find-missing', help='find all missing episode releases in one task; one search per season')
        for finder in (missing, find_missing):
            finder.add_argument('series', nargs='?', help='exact title or unambiguous substring')
            finder.add_argument('--series-id', type=int)
            finder.add_argument('--season', type=int)
            finder.add_argument('--include-unmonitored', action='store_true')
            finder.add_argument('--include-specials', action='store_true')
        find_missing.add_argument('--vostfr', action='store_true', help='strict VOSTFR/SUBFRENCH title matches; other releases remain unverified')
        find_missing.add_argument('--top', type=int, default=2, help='candidates per missing episode (0–10)')
        find_missing.add_argument('--release-file', help='reuse saved native Sonarr releases instead of searching indexers')
        grab = a.add_parser('grab', help='grab the exact selected release through the app')
        grab.add_argument('--guid')
        grab.add_argument('--indexer-id', type=int)
        grab.add_argument('--ref', help='private cached release row, e.g. 123abc456def:7')
        data_flags(grab, fields=True)
        remove = a.add_parser('queue-remove', help='remove a queue record with explicit client/blocklist behavior')
        remove.add_argument('id', type=int)
        remove.add_argument('--remove-from-client', action='store_true')
        remove.add_argument('--blocklist', action='store_true')
        remove.add_argument('--skip-redownload', action='store_true')
        cmd = a.add_parser('command', help='submit any native Arr command and optionally wait for completion')
        cmd.add_argument('name')
        data_flags(cmd, query=False, fields=True)
        mv = a.add_parser('move', help='native Radarr/Sonarr file move; always uses moveFiles=true')
        mv.add_argument('id', type=int)
        target = mv.add_mutually_exclusive_group(required=True)
        target.add_argument('--root', help='existing root folder; retain current folder name')
        target.add_argument('--path', help='exact new destination under an existing app root')
        rename = a.add_parser('rename', help='preview native rename, or apply with --apply')
        rename.add_argument('id', type=int)
        rename.add_argument('--apply', action='store_true')
        manual = a.add_parser('manual-import', help='inspect files, or submit an exact reviewed import command')
        manual.add_argument('--folder')
        manual.add_argument('--apply', action='store_true')
        data_flags(manual, fields=True)
        items = a.add_parser('items', help='Jellyfin media search')
        items.add_argument('--type', dest='item_type', help='Series,Movie,Episode,Season,...')
        items.add_argument('--parent')
        items.add_argument('--search')
        query_flags(items)
        meta = a.add_parser('metadata', help='read or safely edit Jellyfin item metadata, then verify changed fields')
        meta.add_argument('id')
        meta.add_argument('--user-id')
        data_flags(meta, query=False, fields=True)
        images = a.add_parser('images', help='Jellyfin image metadata')
        images.add_argument('id')
        image = a.add_parser('image-set', help='native Jellyfin remote artwork download; replace backdrop 0 by default')
        image.add_argument('id')
        image.add_argument('--type', required=True, dest='image_type', choices=['Primary', 'Logo', 'Backdrop', 'Banner', 'Thumb', 'Art'])
        image.add_argument('--url', required=True)
        image.add_argument('--append', action='store_true', help='append a backdrop instead of replacing it')
        image.add_argument('--index', type=int, default=0)
        refresh = a.add_parser('refresh', help='refresh one Jellyfin item, preserving existing artwork by default')
        refresh.add_argument('id')
        refresh.add_argument('--recursive', action='store_true')
        refresh.add_argument('--replace-metadata', action='store_true')
        refresh.add_argument('--replace-images', action='store_true')
        a.add_parser('libraries', help='Jellyfin library definitions')
        a.add_parser('tasks', help='Jellyfin scheduled tasks')
        task = a.add_parser('task-run', help='start one Jellyfin scheduled task')
        task.add_argument('id')
        for kind in ('collection', 'playlist'):
            collection = a.add_parser(kind, help='create a native Jellyfin ' + kind)
            collection.add_argument('name')
            collection.add_argument('--ids', required=True, help='comma-separated existing Jellyfin item IDs')
            if kind == 'playlist':
                collection.add_argument('--user-id', required=True)
        library = a.add_parser('library-options', help='read or merge edits into a Jellyfin library options object')
        library.add_argument('id')
        data_flags(library, query=False, fields=True)
        subtitles = a.add_parser('subtitles', help='Bazarr subtitle operations (discover exact fields with schema)')
        subtitles.add_argument('--media', choices=['episodes', 'movies'], required=True)
        subtitles.add_argument('--method', choices=['GET', 'POST', 'PATCH', 'DELETE'], default='GET')
        data_flags(subtitles, binary=True)
        search_sub = a.add_parser('subtitle-search', help='Bazarr automatic search/download for a selected language')
        search_sub.add_argument('--series-id', type=int)
        media_id = search_sub.add_mutually_exclusive_group(required=True)
        media_id.add_argument('--episode-id', type=int)
        media_id.add_argument('--movie-id', type=int)
        search_sub.add_argument('--language', default='fr')
        search_sub.add_argument('--forced', action='store_true')
        search_sub.add_argument('--hi', action='store_true')
        sync_sub = a.add_parser('subtitle-sync', help='Bazarr subtitle synchronization through its native tools API')
        sync_sub.add_argument('--type', dest='media_type', choices=['episode', 'movie'], required=True)
        sync_sub.add_argument('--id', type=int, required=True)
        sync_sub.add_argument('--path', required=True, help='subtitle path as seen by Bazarr')
        sync_sub.add_argument('--language', default='fr')
        sync_sub.add_argument('--reference', help='audio stream, e.g. a:0, or a reference subtitle path')
        torrents = a.add_parser('torrents', help='qBittorrent torrent list; accepts native filters and pagination')
        query_flags(torrents)
        a.add_parser('transfer', help='qBittorrent transfer totals and current speeds')
        torrent = a.add_parser('torrent', help='qBittorrent properties, files, or trackers for one hash')
        torrent.add_argument('hash')
        torrent.add_argument('--view', choices=['properties', 'files', 'trackers'], default='properties')
        torrent_action = a.add_parser('torrent-action', help='native qBittorrent control with explicit target hashes')
        torrent_action.add_argument('operation', choices=['start', 'stop', 'recheck', 'reannounce', 'category', 'location', 'delete'])
        torrent_action.add_argument('--hashes', required=True, help='one hash, pipe-separated hashes, or explicitly all')
        torrent_action.add_argument('--category')
        torrent_action.add_argument('--location')
        torrent_action.add_argument('--delete-files', action='store_true')
        search = a.add_parser('search', help='Seerr discovery or Navidrome music search')
        search.add_argument('term')
        query_flags(search)
        # Show only commands relevant to this app. Generic API and discovery
        # remain available even for a separately configured custom instance.
        kind = config.get('apps', {}).get(name, {}).get('kind', name)
        allowed = {'status', 'ops', 'schema', 'call', 'api', 'list', 'get', 'create', 'update', 'delete', 'wait'}
        if kind in ARR:
            allowed |= {'health', 'history', 'profiles', 'tags', 'indexers', 'clients', 'command', 'releases', 'grab'}
        if kind in MANAGERS:
            allowed |= {'queue', 'roots', 'files', 'wanted', 'lookup', 'add', 'queue-remove', 'manual-import'}
        if kind in ('radarr', 'sonarr'):
            allowed |= {'move', 'rename'}
        if kind == 'sonarr':
            allowed |= {'missing', 'find-missing'}
        if kind == 'bazarr':
            allowed |= {'health', 'wanted', 'subtitles', 'subtitle-search', 'subtitle-sync', 'profiles'}
        if kind == 'jellyfin':
            allowed |= {'items', 'metadata', 'images', 'image-set', 'refresh', 'libraries', 'tasks', 'task-run', 'collection', 'playlist', 'library-options'}
            allowed -= {'update'}
        if kind == 'qbittorrent':
            allowed |= {'torrents', 'torrent', 'transfer', 'torrent-action'}
        if kind in ('seerr', 'navidrome'):
            allowed |= {'search'}
        for unsupported in set(a.choices) - allowed:
            del a.choices[unsupported]
        a._choices_actions[:] = [item for item in a._choices_actions if item.dest in allowed]
        for child in a.choices.values():
            # Common flags are parsed globally, but must also be discoverable
            # from action-specific --help, where agents normally look first.
            wait_flags(child)
    args = p.parse_args(remaining)
    vars(args).update(vars(global_args))
    if not math.isfinite(args.timeout) or args.timeout <= 0 or (args.limit is not None and args.limit < 0):
        p.error('--timeout must be positive and --limit cannot be negative')
    try:
        validate_options(args.wait_timeout, args.poll_interval)
        from .output import predicate
        for expression in args.until + args.fail_if:
            predicate(expression)
    except AutoarrError as exc:
        p.error(str(exc))
    action = getattr(args, 'action', None)
    condition = args.wait_path or (action == 'wait' and args.path)
    if condition and not args.until:
        p.error('A condition wait needs at least one --until expression')
    if (args.until or args.fail_if) and not condition:
        p.error('--until/--fail-if require --wait-path or wait --path')
    if args.wait_path and not args.wait:
        p.error('--wait-path requires --wait')
    if args.wait and action != 'wait' and not args.wait_path:
        automatic = action in ('command', 'grab', 'task-run') or (action in ('rename', 'manual-import') and args.apply)
        if not automatic:
            p.error('This action needs --wait-path and --until to verify completion')
    if args.max_output is not None and args.max_output < 256:
        p.error('--max-output must be at least 256 characters')
    if args.scope == 'batch' and not 1 <= args.workers <= 8:
        p.error('--workers must be between 1 and 8')
    if getattr(args, 'action', None) == 'find-missing' and not 0 <= args.top <= 10:
        p.error('--top must be between 0 and 10')
    return args


def json_data(args):
    source = getattr(args, 'data', None)
    if source is None:
        return None
    return load_json(source)


def load_json(source):
    try:
        text = sys.stdin.read() if source == '-' else Path(source[1:]).read_text() if source.startswith('@') else source
        return json.loads(text)
    except (ValueError, OSError):
        raise AutoarrError('Could not read JSON data; use a JSON literal, @file, or - for stdin') from None


def payload(args):
    data = json_data(args)
    fields = getattr(args, 'set', [])
    if fields:
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise AutoarrError('--set requires an object body')
        for key, value in pairs(fields):
            set_path(data, key, value)
    return data


def request_data(args):
    raw = None
    content_type = getattr(args, 'content_type', None)
    if getattr(args, 'upload', None):
        raw = Path(args.upload).read_bytes()
        content_type = content_type or mimetypes.guess_type(args.upload)[0]
        if args.base64_upload:
            raw = base64.b64encode(raw)
    elif getattr(args, 'base64_upload', False):
        raise AutoarrError('--base64-upload requires --upload FILE')
    files = None
    if getattr(args, 'file', None):
        files = []
        for field in args.file:
            if '=' not in field:
                raise AutoarrError('--file needs FIELD=FILE')
            key, name = field.split('=', 1)
            files.append((key, name))
    return {'body': payload(args), 'form': pairs(args.form) if getattr(args, 'form', None) else None, 'files': files,
            'raw': raw, 'content_type': content_type, 'output': getattr(args, 'output', None)}


def condition_target(path, args):
    return {'type': 'condition', 'path': path, 'until': args.until, 'failIf': args.fail_if,
            'query': pairs(getattr(args, 'query', [])) if args.action == 'wait' else []}


def app_action(client, args):
    action = args.action
    if action != 'wait' and not args.wait:
        return _app_action(client, args)
    target = None
    if action == 'wait':
        if args.resume:
            target = json.loads(args.resume)
        elif args.path:
            target = condition_target(args.path, args)
        else:
            for attribute, kind in [('command_id', 'command'), ('download_id', 'download'),
                                    ('torrent_hash', 'torrent'), ('task_id', 'task')]:
                if getattr(args, attribute):
                    target = {'type': kind, 'id': getattr(args, attribute)}
                    if kind == 'download':
                        target['goal'] = args.wait_until
                    break
    elif args.wait_path:
        target = condition_target(args.wait_path, args)
    elif action == 'grab':
        require(client, MANAGERS)
        # The release endpoint may return only the release object. Correlate a
        # new grab using an exact title, indexer, and a pre-write history boundary.
        body = Results(client.state_dir).reference(args.ref, client.app['name']) if args.ref else payload(args) or {}
        args.grab_body = body
        indexer_id = args.indexer_id if args.indexer_id is not None else body.get('indexerId')
        if not body.get('title') or indexer_id is None:
            raise AutoarrError('grab --wait needs a cached release --ref or --data with title and indexerId')
        indexer = client.request('GET', '/indexer/' + str(int(indexer_id)))
        latest = client.request('GET', '/history', query=[('eventType', 1), ('page', 1),
            ('pageSize', 1), ('sortKey', 'date'), ('sortDirection', 'descending')])
        from .output import records
        rows = records(latest)
        if rows is None:
            raise AutoarrError('Cannot establish a grab history boundary; no release was submitted')
        target = {'type': 'release', 'title': body['title'], 'indexer': indexer.get('name'),
                  'afterHistoryId': max((row['id'] for row in rows), default=0), 'goal': args.wait_until}
    elif action == 'task-run':
        require(client, {'jellyfin'})
        from urllib.parse import quote
        before = client.request('GET', '/ScheduledTasks/' + quote(args.id, safe=''))
        if str(before.get('State', '')).casefold() != 'idle':
            raise AutoarrError('Task is already active; use wait --task-id to follow it without starting it again')
        target = {'type': 'task', 'id': args.id, 'baseline': before.get('LastExecutionResult')}
    if target is not None:
        validate_target(client, target)
        if args.no_cache and not args.full and not args.select:
            minimum = {'wait': True, 'status': 'interrupted', 'app': client.app['name'], 'target': target}
            if len(json.dumps(minimum, ensure_ascii=False)) > (args.max_output or 6000):
                raise AutoarrError('Wait target exceeds the output budget; increase --max-output or enable caching before submitting the action')
    if action != 'wait':
        result = _app_action(client, args)
        if client.dry_run:
            return result
        if target is None:
            if not isinstance(result, dict) or result.get('id') is None:
                raise AutoarrError('Command accepted without an ID; inspect native command history before retrying')
            target = {'type': 'command', 'id': result['id']}
        elif target['type'] == 'release' and isinstance(result, dict) and result.get('downloadId'):
            target = {'type': 'download', 'id': result['downloadId'], 'goal': args.wait_until,
                      'afterHistoryId': target['afterHistoryId']}
    args.output_action = 'wait'
    if client.dry_run:
        return {'dryRun': True, 'wait': False, 'target': target}
    return wait_for(client, target, args.wait_timeout, args.poll_interval,
                    args.quiet_progress, getattr(args, 'config_file', None))


def _app_action(client, args):
    action = args.action
    query = pairs(getattr(args, 'query', []))
    if action == 'api':
        return client.request(args.method, args.path, query=query, site=args.site, **request_data(args))
    if action in ('ops', 'schema', 'call'):
        cat = Catalog(client)
        if args.refresh_schema:
            cat.load(refresh=True)
        if action == 'ops':
            return cat.operations(args.search, args.method, details=args.details)
        if action == 'schema':
            return cat.operation(args.operation) if args.operation else cat.load()
        return cat.invoke(args.operation, dict(pairs(args.path)), query, **request_data(args))
    if action == 'status':
        return client.request('GET', client.app.get('status_path', KINDS[client.kind]['status']))
    if action == 'health':
        require(client, ARR | {'bazarr'})
        return client.request('GET', '/system/health' if client.kind == 'bazarr' else '/health')
    if action in ('list', 'get', 'create', 'update', 'delete'):
        path = resource_path(client, args.resource, getattr(args, 'id', None))
        if action in ('list', 'get'):
            return paginated(client, path, server_filters(client, path, query, args.filter), args.all)
        if action == 'create':
            data = payload(args)
            if data is None:
                raise AutoarrError('create requires --data or --set')
            return client.request('POST', path, query=query, body=data)
        if action == 'update':
            if client.kind == 'jellyfin':
                raise AutoarrError('Use metadata for Jellyfin item edits, library-options for libraries, or api for exact native requests')
            if not getattr(args, 'data', None) and not args.set:
                raise AutoarrError('update requires --data or --set')
            before = client.request('GET', path, query=query)
            body = updated_body(before, json_data(args) or {}, args.set)
            if client.kind in ('radarr', 'sonarr') and resource_path(client, args.resource) in ('/movie', '/series'):
                if any(body.get(k) != before.get(k) for k in ('path', 'rootFolderPath')):
                    raise AutoarrError('Use move to change managed paths; it checks the destination and requests moveFiles=true')
            return client.request(args.method, path, query=query, body=body)
        if client.kind in MANAGERS:
            entity = resource_path(client, args.resource).lstrip('/')
            if entity in ('movie', 'series', 'artist', 'author'):
                if not args.id:
                    raise AutoarrError('Deleting managed media requires an explicit ID')
                if any(k == 'deleteFiles' for k, v in query):
                    raise AutoarrError('Use --delete-files to request file deletion explicitly')
                query.append(('deleteFiles', args.delete_files))
        return client.request('DELETE', path, query=query)
    if action in ('queue', 'history', 'roots', 'profiles', 'tags', 'indexers', 'clients', 'files'):
        require(client, ARR | ({'bazarr'} if action == 'profiles' else set()))
        path = {'roots': '/rootfolder', 'profiles': '/qualityprofile', 'tags': '/tag',
                'indexers': '/indexer', 'clients': '/downloadclient'}.get(action, '/' + action)
        if action == 'profiles' and client.kind == 'prowlarr':
            path = '/appprofile'
        if action == 'profiles' and client.kind == 'bazarr':
            path = '/system/languages/profiles'
        if action == 'files':
            require(client, MANAGERS)
            path = {'radarr': '/moviefile', 'sonarr': '/episodefile', 'lidarr': '/trackfile',
                    'bookshelf': '/bookfile', 'readarr': '/bookfile'}[client.kind]
        return paginated(client, path, server_filters(client, path, query, args.filter), args.all)
    if action == 'wanted':
        require(client, MANAGERS | {'bazarr'})
        path = '/' + args.media + '/wanted' if client.kind == 'bazarr' else '/wanted/' + ('cutoff' if args.cutoff else 'missing')
        return paginated(client, path, query, args.all)
    if action == 'lookup':
        require(client, MANAGERS)
        return client.request('GET', '/' + KINDS[client.kind]['entity'] + '/lookup', query=[('term', args.term)] + query)
    if action == 'add':
        return add_media(client, args.term, args.root, args.profile_id,
            args.metadata_profile_id, args.language_profile_id, args.search, args.series_type, args.monitor)
    if action == 'releases':
        require(client, ARR)
        if client.kind == 'prowlarr':
            if not args.term:
                raise AutoarrError('Prowlarr releases needs --term')
            return client.request('GET', '/search', query=[('query', args.term)] + query)
        for attr, native in [('movie_id', 'movieId'), ('series_id', 'seriesId'),
                             ('episode_id', 'episodeId'), ('season_number', 'seasonNumber'),
                             ('artist_id', 'artistId'), ('album_id', 'albumId')]:
            if getattr(args, attr) is not None:
                query.append((native, getattr(args, attr)))
        if not query:
            raise AutoarrError('Supply a movie/series/episode/artist/album ID for release search')
        return client.request('GET', '/release', query=query)
    if action in ('missing', 'find-missing'):
        from .missing import missing, find_missing
        return find_missing(client, args) if action == 'find-missing' else missing(client, args)
    if action == 'grab':
        require(client, ARR)
        if args.ref and (args.data or args.set or args.guid or args.indexer_id is not None):
            raise AutoarrError('Use either --ref or an explicit release body/flags')
        body = args.grab_body if hasattr(args, 'grab_body') else (
            Results(client.state_dir).reference(args.ref, client.app['name']) if args.ref else payload(args) or {})
        if args.guid:
            body['guid'] = args.guid
        if args.indexer_id is not None:
            body['indexerId'] = args.indexer_id
        if not body.get('guid') or body.get('indexerId') is None:
            raise AutoarrError('grab needs a release guid and indexerId, from --data or flags')
        return client.request('POST', '/search' if client.kind == 'prowlarr' else '/release', query=query, body=body)
    if action == 'queue-remove':
        require(client, MANAGERS)
        return client.request('DELETE', '/queue/' + str(args.id), query=[
            ('removeFromClient', args.remove_from_client), ('blocklist', args.blocklist),
            ('skipRedownload', args.skip_redownload)])
    if action == 'command':
        return command(client, args.name, payload(args) or {})
    if action == 'move':
        return move(client, args.id, args.root, args.path)
    if action == 'rename':
        require(client, {'radarr', 'sonarr'})
        if not args.apply:
            key = 'movieId' if client.kind == 'radarr' else 'seriesId'
            return client.request('GET', '/rename', query=[(key, args.id)])
        if client.kind == 'radarr':
            return command(client, 'RenameMovie', {'movieIds': [args.id]})
        return command(client, 'RenameSeries', {'seriesId': args.id})
    if action == 'manual-import':
        require(client, MANAGERS)
        if args.apply:
            data = payload(args)
            if not isinstance(data, dict) or not isinstance(data.get('files'), list) or not data['files']:
                raise AutoarrError('Apply requires an exact reviewed command object containing files; inspect the native ManualImport schema')
            return command(client, 'ManualImport', data)
        if not args.folder:
            raise AutoarrError('Inspect requires --folder')
        return client.request('GET', '/manualimport', query=[('folder', args.folder)] + query)
    if action == 'items':
        require(client, {'jellyfin'})
        query.append(('recursive', True))
        for key, value in [('includeItemTypes', args.item_type), ('parentId', args.parent), ('searchTerm', args.search)]:
            if value is not None:
                query.append((key, value))
        return paginated(client, '/Items', query, args.all)
    if action == 'metadata':
        changes = payload(args) or {}
        if not isinstance(changes, dict):
            raise AutoarrError('Metadata changes must be an object')
        return metadata(client, args.id, changes, args.user_id)
    if action == 'images':
        require(client, {'jellyfin'})
        return client.request('GET', '/Items/' + args.id + '/Images')
    if action == 'image-set':
        return image_set(client, args.id, args.image_type, args.url, args.append, args.index)
    if action == 'refresh':
        require(client, {'jellyfin'})
        return client.request('POST', '/Items/' + args.id + '/Refresh', query=[
            ('recursive', args.recursive), ('metadataRefreshMode', 'FullRefresh' if args.replace_metadata else 'Default'),
            ('imageRefreshMode', 'FullRefresh' if args.replace_images else 'Default'),
            ('replaceAllMetadata', args.replace_metadata), ('replaceAllImages', args.replace_images)])
    if action in ('libraries', 'tasks', 'task-run', 'collection', 'playlist', 'library-options'):
        require(client, {'jellyfin'})
        if action == 'libraries': return client.request('GET', '/Library/VirtualFolders')
        if action == 'tasks': return client.request('GET', '/ScheduledTasks')
        if action == 'task-run': return client.request('POST', '/ScheduledTasks/Running/' + args.id)
        if action == 'collection':
            return client.request('POST', '/Collections', query=[('name', args.name), ('ids', args.ids)])
        if action == 'playlist':
            return client.request('POST', '/Playlists', body={'Name': args.name,
                'Ids': args.ids.split(','), 'UserId': args.user_id, 'MediaType': 'Video', 'IsPublic': False})
        libraries = client.request('GET', '/Library/VirtualFolders')
        matches = [x for x in libraries if x['ItemId'] == args.id]
        if len(matches) != 1:
            raise AutoarrError('No unique library with that ID')
        before = matches[0]['LibraryOptions']
        if not args.data and not args.set:
            return before
        options = updated_body(before, json_data(args) or {}, args.set)
        return client.request('POST', '/Library/VirtualFolders/LibraryOptions', body={'Id': args.id, 'LibraryOptions': options})
    if action == 'subtitles':
        require(client, {'bazarr'})
        return client.request(args.method, '/' + args.media + '/subtitles', query=query, **request_data(args))
    if action == 'subtitle-search':
        require(client, {'bazarr'})
        query = [('language', args.language), ('forced', args.forced), ('hi', args.hi)]
        if args.episode_id is not None:
            if args.series_id is None:
                raise AutoarrError('Episode subtitle search also needs --series-id')
            query += [('seriesid', args.series_id), ('episodeid', args.episode_id)]
            path = '/episodes/subtitles'
        else:
            query.append(('radarrid', args.movie_id))
            path = '/movies/subtitles'
        return client.request('PATCH', path, query=query)
    if action == 'subtitle-sync':
        require(client, {'bazarr'})
        query = [('action', 'sync'), ('language', args.language), ('path', args.path),
                 ('type', args.media_type), ('id', args.id)]
        if args.reference:
            query.append(('reference', args.reference))
        return client.request('PATCH', '/subtitles', query=query)
    if action in ('torrents', 'transfer', 'torrent', 'torrent-action'):
        require(client, {'qbittorrent'})
        if action == 'torrents': return client.request('GET', '/torrents/info', query=query)
        if action == 'transfer': return client.request('GET', '/transfer/info')
        if action == 'torrent': return client.request('GET', '/torrents/' + args.view, query=[('hash', args.hash)])
        operation = {'category': 'setCategory', 'location': 'setLocation'}.get(args.operation, args.operation)
        form = [('hashes', args.hashes)]
        if args.operation == 'category':
            if args.category is None: raise AutoarrError('category requires --category')
            form.append(('category', args.category))
        if args.operation == 'location':
            if not args.location: raise AutoarrError('location requires --location')
            form.append(('location', args.location))
        if args.delete_files and args.operation != 'delete':
            raise AutoarrError('--delete-files is only valid for torrent-action delete')
        if args.operation == 'delete': form.append(('deleteFiles', args.delete_files))
        return client.request('POST', '/torrents/' + operation, form=form)
    if action == 'search':
        require(client, {'seerr', 'navidrome'})
        return client.request('GET', '/search3.view' if client.kind == 'navidrome' else '/search', query=[('query', args.term)] + query)
    raise AutoarrError('Unknown action')


def run(argv, *, parsed=None, config=None, path=None):
    if parsed is None:
        globals_, _ = common_parser().parse_known_args(argv)
        path = config_path(globals_.config)
        config = read_config(path, required=False)
        args = parse(argv, config)
    else:
        args = parsed
    redactor = Redactor(config)
    args.config_file = path
    if args.scope == 'init':
        host = args.host or args.ssh.rsplit('@', 1)[-1]
        if args.dry_run:
            result = {'dryRun': True, 'ssh': args.ssh, 'host': host, 'config': str(path), 'operation': 'read existing credentials; save private local configuration'}
        else:
            result = bootstrap(args.ssh, host, path, args.overwrite)
    elif args.scope == 'config':
        if args.config_action == 'show':
            result = config
        else:
            if args.name in ('init', 'apps', 'doctor', 'config', 'batch', 'result') or not args.name.replace('-', '').replace('_', '').isalnum():
                raise AutoarrError('App name must be alphanumeric with hyphens or underscores, and not a reserved command')
            app = {'kind': args.kind, 'url': args.url}
            for key in ('api_prefix', 'api_key_env', 'auth', 'api_key_header', 'username', 'password_env', 'schema_path', 'status_path', 'user_id'):
                if getattr(args, key, None) is not None:
                    app[key] = getattr(args, key)
            if args.api_key_stdin:
                app['api_key'] = sys.stdin.read().strip()
                if not app['api_key']:
                    raise AutoarrError('No API key received on stdin')
            candidate = {**config, 'apps': {**config['apps'], args.name: app}}
            app_config(candidate, args.name)  # Validate before replacing the configuration.
            if not args.dry_run:
                private_json(path, candidate)
            result = {'configured': args.name, 'kind': args.kind, 'url': args.url, 'config': str(path), 'dryRun': args.dry_run}
    elif args.scope == 'result':
        if args.cached_id:
            entry = Results(path.parent).load(args.cached_id)
            result = entry['data']
            args.output_action = entry.get('action')
            args.result_id = entry['id']
            args.cached_at = entry['createdAt']
        else:
            result = load_json('@' + args.result_file)
            from .output import records
            rows = records(result)
            args.output_action = 'releases' if rows and isinstance(rows[0], dict) and 'guid' in rows[0] else ''
    elif args.scope == 'batch':
        from .batch import execute
        if not path.exists():
            read_config(path)
        result = execute(args, path, config, parse, run, load_json)
        args.output_action = 'batch'
    else:
        if not path.exists():
            read_config(path)
        def client(name):
            return Client(app_config(config, name), path.parent, redactor, args.timeout, args.dry_run)
        if args.scope == 'apps':
            result = [{'name': name, 'kind': c.kind, 'url': c.app['url'], 'apiPrefix': c.app['api_prefix'],
                       'auth': c.app['auth'], 'credentialConfigured': bool(c.app.get('api_key') or c.app.get('password') or c.app['auth'] == 'none')}
                      for name in sorted(config['apps']) for c in [client(name)]]
        elif args.scope == 'doctor':
            clients = [(name, client(name)) for name in sorted(config['apps'])]
            def probe(pair):
                name, c = pair
                try:
                    data = c.request('GET', c.app.get('status_path', KINDS[c.kind]['status']))
                    if isinstance(data, dict) and 'error' in data and data['error']:
                        raise AutoarrError('Application returned an error')
                    version = data.get('version', data.get('Version', data.get('result', {}).get('version') if isinstance(data.get('result'), dict) else None)) if isinstance(data, dict) else data
                    if c.kind == 'bazarr' and isinstance(data, dict):
                        version = data.get('data', {}).get('bazarr_version')
                    return {'app': name, 'ok': True, 'version': version}
                except AutoarrError as e:
                    return {'app': name, 'ok': False, 'error': redactor.clean(str(e))}
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                result = list(pool.map(probe, clients))
        else:
            result = app_action(client(args.scope), args)
    if not args.no_cache and args.scope not in ('config', 'init', 'apps', 'doctor') and not getattr(args, 'result_id', None):
        try:
            action = getattr(args, 'output_action', getattr(args, 'action', args.scope))
            args.result_id = Results(path.parent).save(result, args.scope, action)
        except OSError:
            args.cache_error = 'Result cache unavailable; the application request has already returned. Do not repeat a write just to cache its response.'
    redactor.collect(result)
    return redactor, args, result


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        redactor, args, result = run(argv)
        print(render_result(redactor.clean(result), args, getattr(args, 'result_id', None), getattr(args, 'output_action', None)))
        if args.scope == 'doctor' and any(not r['ok'] for r in result):
            return 1
        if args.scope == 'batch' and result['failed']:
            return 1
        return wait_exit_code(result)
    except (AutoarrError, ValueError, OSError, KeyboardInterrupt) as e:
        # Re-read only the local config for redaction; never print a raw traceback,
        # request object, HTTP authorization header, or bootstrap stdout.
        try:
            opts, _ = common_parser().parse_known_args(argv)
            cfg = read_config(config_path(opts.config), required=False)
            clean = Redactor(cfg)
            for name in cfg.get('apps', {}):
                clean.collect(app_config(cfg, name))
        except (AutoarrError, OSError):
            clean = Redactor()
        message = 'Interrupted; inspect the app before retrying a write' if isinstance(e, KeyboardInterrupt) else str(e)
        print(json.dumps({'error': clean.clean(message)}, ensure_ascii=False), file=sys.stderr)
        return 130 if isinstance(e, KeyboardInterrupt) else 2
