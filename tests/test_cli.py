"""HTTP integration tests. These never contact the real media server."""
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from autoarr.catalog import Catalog
from autoarr.cli import main
from autoarr.client import ApiError, Client
from autoarr.config import AutoarrError, app_config, private_json, read_config
from autoarr.output import format_result
from autoarr.security import Redactor
from autoarr.workflows import add_media, command, image_set, metadata, move, paginated

KEY = 'test-only-api-key-never-a-real-credential'


class Handler(BaseHTTPRequestHandler):
    routes = {}
    calls = []

    def log_message(self, *args):
        pass

    def handle_request(self):
        parts = urlsplit(self.path)
        raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        try:
            body = json.loads(raw) if raw else None
        except (ValueError, UnicodeDecodeError):
            body = raw
        call = {'method': self.command, 'path': parts.path,
                'query': parse_qs(parts.query, keep_blank_values=True),
                'body': body, 'headers': dict(self.headers)}
        Handler.calls.append(call)
        route = Handler.routes.get((self.command, parts.path))
        response = route(call) if callable(route) else route
        if response is None:
            response = (404, {'error': 'fixture endpoint not found'})
        status, data, *extra = response
        headers = extra[0] if extra else {}
        content_type = headers.get('Content-Type', 'application/json')
        if not isinstance(data, bytes):
            data = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        for k, v in headers.items():
            if k != 'Content-Type': self.send_header(k, v)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_request


class HttpFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        Handler.routes = {}
        Handler.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, kind='sonarr', dry_run=False, **changes):
        prefix = {'sonarr': '/api/v3', 'radarr': '/api/v3', 'qbittorrent': '/api/v2',
                  'bazarr': '/api', 'jellyfin': '', 'navidrome': '/rest'}[kind]
        app = {'name': kind, 'kind': kind, 'url': self.url, 'api_prefix': prefix,
               'auth': 'jellyfin' if kind == 'jellyfin' else 'none' if kind == 'qbittorrent' else 'header',
               'api_key': KEY, **changes}
        return Client(app, self.directory, Redactor({'api_key': KEY}), dry_run=dry_run, timeout=2)

    def route(self, method, path, data, status=200, headers=None):
        Handler.routes[(method, path)] = (status, data, headers or {})

    def move_fixture(self, collision=False):
        self.route('GET', '/api/v3/series/7', {'id': 7, 'title': 'Show', 'path': '/media/Anime/Show', 'monitored': True})
        self.route('GET', '/api/v3/rootfolder', [{'id': 1, 'path': '/media/Series'}])
        def listing(call):
            self.assertEqual(call['query']['path'], ['/media/Series/'])
            return 200, {'parent': '/media/', 'directories': [{'path': '/media/Series/Show/'}] if collision else [], 'files': []}
        Handler.routes[('GET', '/api/v3/filesystem')] = listing
        Handler.routes[('PUT', '/api/v3/series/7')] = lambda c: (200, c['body'])

    def cli_config(self, kind='sonarr'):
        path = self.directory / 'cli-config.json'
        private_json(path, {'version': 1, 'apps': {kind: {
            'kind': kind, 'url': self.url, 'api_key': KEY}}})
        return path


class ApiTests(HttpFixture):

    def test_base_path_and_api_prefix_are_preserved_once(self):
        c = self.client(url=self.url + '/sonarr')
        self.route('GET', '/sonarr/api/v3/series', [{'id': 1}])
        self.assertEqual(c.request('GET', '/api/v3/series'), [{'id': 1}])
        self.assertEqual(Handler.calls[-1]['headers']['X-Api-Key'], KEY)

    def test_repeated_query_and_native_boolean_serialization(self):
        self.route('GET', '/api/v3/queue', {})
        self.client().request('GET', '/queue?existing=1', query=[('id', 1), ('id', 2), ('flag', False)])
        self.assertEqual(Handler.calls[0]['query'], {'existing': ['1'], 'id': ['1', '2'], 'flag': ['false']})

    def test_arbitrary_native_endpoint_is_available(self):
        self.route('PATCH', '/api/v3/new-endpoint', {'ok': True})
        self.assertTrue(self.client().request('PATCH', '/new-endpoint', body={'futureField': 2})['ok'])

    def test_dry_run_never_sends_a_write(self):
        for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            result = self.client(dry_run=True).request(method, '/queue/1', body={'apiKey': KEY})
            self.assertTrue(result['dryRun'])
            self.assertNotIn(KEY, json.dumps(result))
        self.assertEqual(Handler.calls, [])

    def test_dry_run_move_uses_native_mover_and_checks_destination(self):
        self.move_fixture()
        result = move(self.client(dry_run=True), 7, root='/media/Series')
        self.assertEqual(result['request']['query'], [['moveFiles', True]])
        self.assertEqual(result['request']['body']['path'], '/media/Series/Show')
        self.assertTrue(all(c['method'] == 'GET' for c in Handler.calls))

    def test_actual_move_submits_move_files_true(self):
        self.move_fixture()
        result = move(self.client(), 7, root='/media/Series')
        write = Handler.calls[-1]
        self.assertEqual(write['method'], 'PUT')
        self.assertEqual(write['query']['moveFiles'], ['true'])
        self.assertTrue(write['body']['monitored'])
        self.assertTrue(result['moveFiles'])

    def test_move_refuses_existing_directory_without_overwrite(self):
        self.move_fixture(collision=True)
        with self.assertRaisesRegex(AutoarrError, 'already exists'):
            move(self.client(), 7, root='/media/Series')
        self.assertTrue(all(c['method'] == 'GET' for c in Handler.calls))

    def test_move_refuses_wrong_filesystem_listing(self):
        self.move_fixture()
        self.route('GET', '/api/v3/filesystem', {'parent': '/wrong/', 'directories': []})
        with self.assertRaisesRegex(AutoarrError, 'did not list'):
            move(self.client(), 7, root='/media/Series')

    def test_jellyfin_metadata_preserves_language_and_omits_read_only_fields(self):
        item = {'Id': 'show', 'Name': 'Old', 'OriginalLanguage': 'ja', 'Overview': 'Synopsis',
                'ProviderIds': {'Autoarr': 'custom'}, 'TrickplayInfo': {'breaks': 'roundtrip'},
                'UserData': {'IsFavorite': True}}
        self.route('GET', '/Items/show', item)
        def update(call):
            self.assertEqual(call['body']['OriginalLanguage'], 'ja')
            self.assertNotIn('TrickplayInfo', call['body'])
            self.assertNotIn('UserData', call['body'])
            self.assertNotIn('ProviderIds', call['body'])
            item.update(call['body'])
            return 204, b''
        Handler.routes[('POST', '/Items/show')] = update
        result = metadata(self.client('jellyfin'), 'show', {'OriginalTitle': 'Monogatari Series'}, user_id='user')
        self.assertEqual(result['item']['OriginalTitle'], 'Monogatari Series')
        self.assertEqual(item['Name'], 'Old')
        self.assertEqual(Handler.calls[0]['headers']['Authorization'], f'MediaBrowser Token="{KEY}"')

    def test_metadata_rejects_read_only_or_id_updates(self):
        for field in ('Id', 'TrickplayInfo'):
            with self.assertRaises(AutoarrError):
                metadata(self.client('jellyfin'), 'show', {field: 'bad'}, user_id='user')
        self.assertEqual(Handler.calls, [])

    def test_backdrop_replacement_removes_only_old_image_after_swap(self):
        images = [{'ImageType': 'Backdrop', 'ImageIndex': 0}]
        Handler.routes[('GET', '/Items/show/Images')] = lambda c: (200, images[:])
        def download(call):
            images.append({'ImageType': 'Backdrop', 'ImageIndex': 1})
            return 204, b''
        Handler.routes[('POST', '/Items/show/RemoteImages/Download')] = download
        self.route('POST', '/Items/show/Images/Backdrop/1/Index', b'', 204)
        def remove(call):
            images.pop()
            return 204, b''
        Handler.routes[('DELETE', '/Items/show/Images/Backdrop/1')] = remove
        image_set(self.client('jellyfin'), 'show', 'Backdrop', 'https://example.org/art.jpg')
        writes = [(c['method'], c['path']) for c in Handler.calls if c['method'] != 'GET']
        self.assertEqual(writes, [('POST', '/Items/show/RemoteImages/Download'),
            ('POST', '/Items/show/Images/Backdrop/1/Index'), ('DELETE', '/Items/show/Images/Backdrop/1')])

    def test_multipart_upload_preserves_file_bytes(self):
        file = self.directory / 'sub.srt'
        file.write_bytes(b'1\n00:00:00,000 --> 00:00:01,000\nBonjour\n')
        self.route('POST', '/api/episodes/subtitles', {'ok': True})
        self.client('bazarr').request('POST', '/episodes/subtitles', files=[('file', file)], form=[('language', 'fr')])
        call = Handler.calls[-1]
        self.assertIn('multipart/form-data; boundary=', call['headers']['Content-Type'])
        self.assertIn(file.read_bytes(), call['body'])
        self.assertIn(b'name="file"; filename="sub.srt"', call['body'])

    def test_qbittorrent_uses_form_body(self):
        self.route('POST', '/api/v2/torrents/setCategory', b'Ok.', headers={'Content-Type': 'text/plain'})
        self.client('qbittorrent').request('POST', '/torrents/setCategory', form=[('hashes', 'abc|def'), ('category', 'sonarr')])
        self.assertEqual(parse_qs(Handler.calls[-1]['body'].decode()), {'hashes': ['abc|def'], 'category': ['sonarr']})
        with self.assertRaisesRegex(AutoarrError, 'expects --form'):
            self.client('qbittorrent').request('POST', '/torrents/stop', body={'hashes': 'abc'})

    def test_write_journal_redacts_credential_fields_and_urls(self):
        self.route('POST', '/api/v3/config', {'ok': True})
        self.client().request('POST', '/config', body={'apiKey': KEY,
            'fields': [{'name': 'password', 'value': 'different-sensitive-value'}],
            'url': 'https://example.org/download?apikey=tracker-secret'})
        text = (self.directory / 'requests.jsonl').read_text()
        for secret in (KEY, 'different-sensitive-value', 'tracker-secret'):
            self.assertNotIn(secret, text)
        self.assertEqual((self.directory / 'requests.jsonl').stat().st_mode & 0o777, 0o600)

    def test_http_errors_are_sanitized(self):
        self.route('GET', '/api/v3/error', {'message': 'Echo: ' + KEY, 'password': 'another-secret'}, 400)
        with self.assertRaises(ApiError) as e:
            self.client().request('GET', '/error')
        self.assertNotIn(KEY, str(e.exception))
        self.assertNotIn('another-secret', str(e.exception))

    def test_foreign_redirect_does_not_forward_credentials(self):
        self.route('GET', '/api/v3/redirect', b'', 302, {'Location': 'http://localhost:1/capture'})
        with self.assertRaisesRegex(AutoarrError, 'cross-origin'):
            self.client().request('GET', '/redirect')
        self.assertEqual(len(Handler.calls), 1)

    def test_absolute_and_traversal_paths_are_rejected(self):
        for path in ('https://example.org/capture', '//example.org', '/../settings', '/%252e%252e/settings', '/back\\slash'):
            with self.assertRaises(AutoarrError): self.client().request('GET', path)
        self.assertEqual(Handler.calls, [])

    def test_html_login_response_is_not_reported_as_api_success(self):
        self.route('GET', '/api/v3/system/status', b'<html>login</html>', headers={'Content-Type': 'text/html'})
        with self.assertRaisesRegex(AutoarrError, 'returned HTML'):
            self.client().request('GET', '/system/status')

    def test_binary_export_is_private_and_does_not_overwrite(self):
        data = b'\x89PNG\x00\xff'
        self.route('GET', '/Items/show/Images/Logo', data, headers={'Content-Type': 'image/png'})
        file = self.directory / 'logo.png'
        result = self.client('jellyfin').request('GET', '/Items/show/Images/Logo', output=file)
        self.assertEqual(file.read_bytes(), data)
        self.assertEqual(result['bytes'], len(data))
        self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(AutoarrError):
            self.client('jellyfin').request('GET', '/Items/show/Images/Logo', output=file)

    def test_pagination_collects_all_records(self):
        Handler.routes[('GET', '/api/v3/queue')] = lambda c: (200, {'records': [{'id': 1}] if c['query']['page'] == ['1'] else [{'id': 2}], 'totalRecords': 2})
        result = paginated(self.client(), '/queue', all_pages=True)
        self.assertEqual([r['id'] for r in result['records']], [1, 2])
        self.assertEqual(result['autoarrPagination']['pages'], 2)

    def test_pagination_detects_ignored_parameters(self):
        self.route('GET', '/api/v3/queue', {'records': [{'id': 1}], 'totalRecords': 10})
        with self.assertRaisesRegex(AutoarrError, 'repeated a page'):
            paginated(self.client(), '/queue', all_pages=True)

    def test_discovered_call_validates_path_query_and_request_body(self):
        spec = {'openapi': '3.0.1', 'paths': {'/api/v3/movie/{id}': {'put': {'operationId': 'editMovie',
            'parameters': [{'name': 'id', 'in': 'path', 'required': True}, {'name': 'moveFiles', 'in': 'query', 'required': True}],
            'requestBody': {'required': True, 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Movie'}}}}}}},
            'components': {'schemas': {'Movie': {'type': 'object', 'properties': {'path': {'type': 'string'}}}}}}
        self.route('GET', '/api/v3/openapi.json', spec)
        c = self.client(dry_run=True)
        catalog = Catalog(c)
        with self.assertRaisesRegex(AutoarrError, 'Path parameters'):
            catalog.invoke('editMovie', {}, [])
        with self.assertRaisesRegex(AutoarrError, 'query parameter'):
            catalog.invoke('editMovie', {'id': 2}, [])
        with self.assertRaisesRegex(AutoarrError, 'requires --data'):
            catalog.invoke('editMovie', {'id': 2}, [('moveFiles', True)])
        result = catalog.invoke('editMovie', {'id': 2}, [('moveFiles', True)], body={'path': '/new'})
        self.assertEqual(result['request']['url'], self.url + '/api/v3/movie/2')
        self.assertEqual(catalog.operation('editMovie')['requestBody']['content']['application/json']['schema']['type'], 'object')

    def test_command_wait_reports_failed_jobs(self):
        self.route('POST', '/api/v3/command', {'id': 5})
        self.route('GET', '/api/v3/command/5', {'id': 5, 'status': 'failed', 'message': 'Bad source'})
        with self.assertRaisesRegex(AutoarrError, 'did not complete'):
            command(self.client(), 'ManualImport', {'files': []}, wait=True)

    def test_add_refuses_ambiguous_search_results(self):
        self.route('GET', '/api/v3/rootfolder', [{'path': '/media/Series'}])
        self.route('GET', '/api/v3/qualityprofile', [{'id': 1}])
        self.route('GET', '/api/v3/series/lookup', [{'title': 'Show'}, {'title': 'Show 2'}])
        with self.assertRaisesRegex(AutoarrError, '2 candidates'):
            add_media(self.client(), 'Show', '/media/Series', 1)
        self.assertTrue(all(c['method'] == 'GET' for c in Handler.calls))

    def test_nav_subsonic_get_mutation_obeys_dry_run(self):
        result = self.client('navidrome', dry_run=True, auth='subsonic').request('GET', '/deletePlaylist.view', query=[('id', 1)])
        self.assertTrue(result['dryRun'])
        self.assertEqual(Handler.calls, [])

    def test_config_permissions_and_environment_override(self):
        path = self.directory / 'config.json'
        config = {'version': 1, 'apps': {'sonarr': {'kind': 'sonarr', 'url': self.url, 'api_key_env': 'TEST_AUTOARR_KEY'}}}
        private_json(path, config)
        with patch.dict(os.environ, {'TEST_AUTOARR_KEY': KEY}):
            self.assertEqual(app_config(read_config(path), 'sonarr')['api_key'], KEY)
        self.assertEqual(app_config({'apps': {'autobrr': {'kind': 'autobrr', 'url': self.url}}}, 'autobrr')['api_key_header'], 'X-API-Token')
        os.chmod(path, 0o644)
        with self.assertRaisesRegex(AutoarrError, 'must be private'):
            read_config(path)

    def test_url_validation_catches_malformed_port(self):
        config = {'apps': {'sonarr': {'kind': 'sonarr', 'url': 'http://localhost:8989sonarr'}}}
        with self.assertRaisesRegex(AutoarrError, 'invalid port'):
            app_config(config, 'sonarr')

    def test_private_config_writer_refuses_symlink(self):
        target = self.directory / 'target'
        target.write_text('unchanged')
        link = self.directory / 'config.json'
        link.symlink_to(target)
        with self.assertRaises(AutoarrError): private_json(link, {'secret': 'no'})
        self.assertEqual(target.read_text(), 'unchanged')

    def test_cli_flags_work_after_actions_and_no_ssh_is_used(self):
        cfg = self.directory / 'config.json'
        private_json(cfg, {'version': 1, 'apps': {'tv': {'kind': 'sonarr', 'url': self.url, 'api_key': KEY}}})
        self.route('GET', '/api/v3/series', [{'id': 1, 'title': 'Monogatari'}, {'id': 2, 'title': 'Other'}])
        stdout = io.StringIO()
        with patch('subprocess.run', side_effect=AssertionError('Normal CLI must not use SSH')):
            with contextlib.redirect_stdout(stdout):
                code = main(['tv', 'list', 'series', '--config', str(cfg), '--filter', 'title~=mono', '--select', 'id,title'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())['rows'], [{'id': 1, 'title': 'Monogatari'}])
        self.assertNotIn(KEY, stdout.getvalue())

    def test_table_escapes_terminal_controls_and_nested_select_works(self):
        text = format_result([{'title': 'X\x1b[31m', 'stats': {'files': 3}}], select='title,stats.files', table=True)
        self.assertNotIn('\x1b', text)
        self.assertIn('stats.files', text)
        self.assertIn('3', text)

    def test_generic_update_cannot_silently_change_managed_paths(self):
        cfg = self.cli_config()
        self.route('GET', '/api/v3/series/7', {'id': 7, 'path': '/old', 'rootFolderPath': '/root'})
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--config', str(cfg), 'sonarr', 'update', 'series', '7', '--set', 'path=/new'])
        self.assertEqual(code, 2)
        self.assertIn('Use move', err.getvalue())
        self.assertTrue(all(c['method'] == 'GET' for c in Handler.calls))

    def test_discovery_can_call_site_routes_outside_api_prefix(self):
        spec = {'openapi': '3.0.1', 'paths': {'/api/v3/series': {'get': {}}, '/login': {'get': {'operationId': 'loginPage'}}}}
        self.route('GET', '/api/v3/openapi.json', spec)
        self.route('GET', '/login', {'fixture': True})
        self.assertTrue(Catalog(self.client()).invoke('loginPage', {}, [])['fixture'])
        self.assertEqual(Handler.calls[-1]['path'], '/login')

    def test_discovered_query_arrays_are_serialized_from_schema(self):
        spec = {'openapi': '3.0.1', 'paths': {'/api/v3/rename': {'get': {'operationId': 'renamePreview',
            'parameters': [{'name': 'movieId', 'in': 'query', 'schema': {'type': 'array', 'items': {'type': 'integer'}}}]}}}}
        self.route('GET', '/api/v3/openapi.json', spec)
        self.route('GET', '/api/v3/rename', [])
        Catalog(self.client()).invoke('renamePreview', {}, [('movieId', [1, 2])])
        self.assertEqual(Handler.calls[-1]['query']['movieId'], ['1', '2'])

    def test_required_multipart_fields_are_checked_before_writing(self):
        spec = {'swagger': '2.0', 'paths': {'/upload': {'post': {'operationId': 'upload',
            'parameters': [{'name': 'file', 'in': 'formData', 'required': True, 'type': 'file'}]}}}}
        self.route('GET', '/api/swagger.json', spec)
        with self.assertRaisesRegex(AutoarrError, 'form field'):
            Catalog(self.client('bazarr')).invoke('upload', {}, [])
        self.assertTrue(all(c['method'] == 'GET' for c in Handler.calls))

    def test_app_help_only_shows_applicable_shortcuts(self):
        cfg = self.cli_config()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as e:
            main(['--config', str(cfg), 'sonarr', '--help'])
        self.assertEqual(e.exception.code, 0)
        self.assertIn('move', output.getvalue())
        self.assertNotIn('subtitle-sync', output.getvalue())
        self.assertNotIn('torrent-action', output.getvalue())

    def test_tracker_credentials_are_redacted_even_in_other_fields(self):
        value = {'fields': [{'name': 'passkey', 'value': 'tracker-key-only-for-tests'}],
                 'downloadUrl': 'https://example.org/tracker-key-only-for-tests/download',
                 'api_key_env': 'SONARR_API_KEY'}
        redactor = Redactor()
        redactor.collect(value)
        result = redactor.clean(value)
        self.assertNotIn('tracker-key-only-for-tests', json.dumps(result))
        self.assertEqual(result['api_key_env'], 'SONARR_API_KEY')

    def test_torrent_delete_defaults_to_keep_files(self):
        cfg = self.cli_config('qbittorrent')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(['--config', str(cfg), 'qbittorrent', 'torrent-action', 'delete', '--hashes', 'hash', '--dry-run'])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertIn(['deleteFiles', False], result['request']['form'])
        self.assertEqual(Handler.calls, [])


if __name__ == '__main__':
    unittest.main()
