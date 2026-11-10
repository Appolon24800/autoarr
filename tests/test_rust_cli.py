"""End-to-end tests of the Rust executable against the existing local API fixtures.

Build first: cargo build --release --locked
The Python implementation remains a reference, not a subprocess used by Rust.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import test_cli as api
import test_query_batch as query
import test_optimization as optimization
import test_waiting as waiting

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get('AUTOARR_TEST_BIN', ROOT / 'target/release/autoarr'))


def native_main(argv=None):
    argv = list(argv or [])
    # Real short waits exercise the native process; Python's virtual clock is
    # deliberately not used to simulate a Rust result.
    if ('wait' in argv or '--wait' in argv) and '--poll-interval' not in argv:
        argv += ['--poll-interval', '0.005']
    source = sys.stdin.read() if '-' in argv and isinstance(sys.stdin, io.StringIO) else None
    process = subprocess.Popen([str(BINARY), *argv], stdin=subprocess.PIPE if source is not None else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = process.communicate(source, timeout=15)
    print(out, end='')
    print(err, end='', file=sys.stderr)
    if '--help' in argv or '-h' in argv:
        raise SystemExit(process.returncode)
    return process.returncode


@unittest.skipUnless(BINARY.exists(), 'cargo build --release --locked is required')
class RustWorkflowTests(api.HttpFixture):
    invoke = query.WorkflowTests.invoke
    missing_fixture = query.WorkflowTests.missing_fixture

    def setUp(self):
        super().setUp()
        adapter = patch.object(query, 'main', native_main)
        adapter.start()
        self.addCleanup(adapter.stop)


for name in (
    'test_result_replay_never_calls_the_server',
    'test_grab_by_reference_sends_original_payload_without_printing_it',
    'test_batch_reads_across_multiple_apps_and_ids',
    'test_parent_dry_run_and_no_cache_propagate_to_every_job',
    'test_batch_stops_mutations_after_error_and_reports_skipped_jobs',
    'test_invalid_batch_connections_fail_before_any_writes',
    'test_json_foreach_is_one_task_and_preserves_typed_values',
    'test_cached_schema_pushes_native_filters_and_retains_local_filtering',
    'test_find_missing_searches_once_per_season_and_never_grabs',
    'test_find_missing_reuses_export_without_search_and_no_cache_is_respected',
):
    setattr(RustWorkflowTests, name, getattr(query.WorkflowTests, name))


@unittest.skipUnless(BINARY.exists(), 'cargo build --release --locked is required')
class RustLegacyCliTests(api.HttpFixture):
    def setUp(self):
        super().setUp()
        adapter = patch.object(api, 'main', native_main)
        adapter.start()
        self.addCleanup(adapter.stop)


for name in ('test_cli_flags_work_after_actions_and_no_ssh_is_used',
             'test_generic_update_cannot_silently_change_managed_paths',
             'test_app_help_only_shows_applicable_shortcuts',
             'test_torrent_delete_defaults_to_keep_files'):
    setattr(RustLegacyCliTests, name, getattr(api.ApiTests, name))


@unittest.skipUnless(BINARY.exists(), 'cargo build --release --locked is required')
class RustBatchTests(api.HttpFixture):
    invoke = optimization.OptimizationTests.invoke
    source = optimization.OptimizationTests.source

    def setUp(self):
        super().setUp()
        adapter = patch.object(optimization, 'main', native_main)
        adapter.start()
        self.addCleanup(adapter.stop)


for name in dir(optimization.OptimizationTests):
    if name.startswith('test_') and name != 'test_static_batch_parses_each_child_once_and_reads_config_once':
        setattr(RustBatchTests, name, getattr(optimization.OptimizationTests, name))


@unittest.skipUnless(BINARY.exists(), 'cargo build --release --locked is required')
class RustWaitTests(api.HttpFixture):
    invoke = waiting.WaitTests.invoke
    sequence = waiting.WaitTests.sequence
    queue = waiting.WaitTests.queue
    history = waiting.WaitTests.history
    grab_fixture = waiting.WaitTests.grab_fixture

    def setUp(self):
        super().setUp()
        adapter = patch.object(waiting, 'main', native_main)
        adapter.start()
        self.addCleanup(adapter.stop)

    def test_native_wait_streams_progress_and_submits_only_once(self):
        self.route('POST', '/api/v3/command', {'id': 5})
        self.sequence('/api/v3/command/5', [{'id': 5, 'status': s} for s in ('queued', 'started', 'completed')])
        code, data, stderr = self.invoke('sonarr', 'command', 'RefreshSeries', '--wait', '--wait-timeout', '1')
        self.assertEqual((code, data['status'], data['polls']), (0, 'completed', 3))
        self.assertEqual(len([c for c in api.Handler.calls if c['method'] == 'POST']), 1)
        self.assertEqual([json.loads(line)['status'] for line in stderr.splitlines()], ['pending', 'pending', 'completed'])

    def test_native_interrupt_returns_readonly_resume_without_cancelling(self):
        cfg = self.cli_config()
        self.route('GET', '/api/v3/command/5', {'status': 'started'})
        process = subprocess.Popen([str(BINARY), '--config', str(cfg), 'sonarr', 'wait', '--command-id', '5',
                                    '--poll-interval', '0.005'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 3
            while not api.Handler.calls and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertTrue(api.Handler.calls)
            process.send_signal(signal.SIGINT)
            out, _ = process.communicate(timeout=3)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        data = json.loads(out)
        self.assertEqual((process.returncode, data['status']), (130, 'interrupted'))
        self.assertIn('--resume', data['resume'])
        self.assertTrue(all(c['method'] == 'GET' for c in api.Handler.calls))

    def test_native_timeout_does_not_repeat_the_original_write(self):
        self.route('POST', '/api/v3/command', {'id': 5})
        self.route('GET', '/api/v3/command/5', {'status': 'started'})
        code, data, _ = self.invoke('sonarr', 'command', 'RefreshSeries', '--wait', '--wait-timeout', '0.04')
        self.assertEqual((code, data['status']), (3, 'timeout'))
        self.route('GET', '/api/v3/command/5', {'status': 'completed'})
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(native_main(data['resume'][1:]), 0)
        self.assertEqual(len([c for c in api.Handler.calls if c['method'] == 'POST']), 1)


for name in (
    'test_failure_and_abort_are_nonzero_and_resumable', 'test_dry_run_does_not_poll_or_submit',
    'test_zero_timeout_returns_target_without_polling', 'test_quiet_progress_keeps_stdout_machine_readable',
    'test_progress_is_redacted_and_terminal_controls_are_escaped', 'test_temporary_http_errors_retry_reads_only',
    'test_http_failure_after_submit_preserves_target', 'test_download_waits_for_import_instead_of_transfer_completion',
    'test_downloaded_goal_returns_before_import', 'test_empty_queue_and_unrelated_import_are_not_success',
    'test_blocked_import_fails_with_native_reason', 'test_download_wait_searches_all_queue_pages',
    'test_season_pack_requires_every_grabbed_episode_import',
    'test_grab_without_download_id_is_correlated_and_never_repeated', 'test_grab_body_from_stdin_is_only_read_once',
    'test_ambiguous_grabs_fail_instead_of_guessing', 'test_incomplete_release_tracking_is_rejected_before_grab',
    'test_release_resume_survives_long_titles_without_truncation', 'test_small_output_budget_keeps_status_and_readonly_target',
    'test_oversized_target_remains_available_in_private_snapshot', 'test_torrent_completion_waits_for_checking_and_moving',
    'test_missing_or_failed_torrent_is_not_success', 'test_task_run_ignores_previous_success_and_waits_for_new_execution',
    'test_task_failed_execution_is_nonzero', 'test_following_running_task_preserves_execution_baseline_on_resume',
    'test_history_from_previous_grab_of_same_hash_is_not_completion',
    'test_generic_condition_waits_after_one_write_and_failure_wins', 'test_generic_conditions_use_raw_response_not_display_selection',
    'test_generic_envelope_conditions_can_wait_for_empty_queue', 'test_oversized_uncached_target_is_rejected_before_write',
    'test_action_help_explains_wait_and_condition_options', 'test_resume_cannot_forward_credentials_to_foreign_url',
    'test_batch_stops_followup_write_after_wait_timeout',
):
    setattr(RustWaitTests, name, getattr(waiting.WaitTests, name))


@unittest.skipUnless(BINARY.exists(), 'cargo build --release --locked is required')
class RustTransportTests(api.HttpFixture):
    def invoke(self, kind, *argv, source=None):
        cfg = self.cli_config(kind)
        process = subprocess.run([str(BINARY), '--config', str(cfg), kind, *argv], input=source,
                                 capture_output=True, text=True, timeout=10)
        return process.returncode, json.loads(process.stdout) if process.stdout else None, process.stderr

    def test_auth_headers_and_base_paths(self):
        cfg = self.directory / 'base.json'
        from autoarr.config import private_json
        private_json(cfg, {'version': 1, 'apps': {'tv': {'kind': 'sonarr', 'url': self.url + '/sonarr', 'api_key': api.KEY}}})
        self.route('GET', '/sonarr/api/v3/series', [])
        p = subprocess.run([str(BINARY), '--config', str(cfg), 'tv', 'api', 'GET', '/api/v3/series'], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(api.Handler.calls[0]['headers']['x-api-key'], api.KEY)

    def test_native_move_submits_movefiles_true(self):
        self.move_fixture()
        code, data, stderr = self.invoke('sonarr', 'move', '7', '--root', '/media/Series')
        self.assertEqual((code, data['moveFiles']), (0, True), stderr)
        self.assertEqual(api.Handler.calls[-1]['query']['moveFiles'], ['true'])
        self.assertEqual(api.Handler.calls[-1]['body']['path'], '/media/Series/Show')

    def test_move_collision_and_wrong_listing_do_not_write(self):
        self.move_fixture(collision=True)
        code, _, _ = self.invoke('sonarr', 'move', '7', '--root', '/media/Series')
        self.assertEqual(code, 2)
        self.assertTrue(all(c['method'] == 'GET' for c in api.Handler.calls))
        self.route('GET', '/api/v3/filesystem', {'parent': '/wrong', 'directories': [], 'files': []})
        code, _, _ = self.invoke('sonarr', 'move', '7', '--root', '/media/Series')
        self.assertEqual(code, 2)
        self.assertTrue(all(c['method'] == 'GET' for c in api.Handler.calls))

    def test_foreign_redirect_never_receives_auth(self):
        self.route('GET', '/api/v3/health', {}, status=302, headers={'Location': 'http://127.0.0.1:1/steal'})
        code, _, stderr = self.invoke('sonarr', 'health')
        self.assertEqual(code, 2)
        self.assertIn('cross-origin', stderr)
        self.assertNotIn(api.KEY, stderr)
        self.assertEqual(len(api.Handler.calls), 1)

    def test_write_redirects_are_not_followed(self):
        self.route('POST', '/api/v3/command', {}, status=307, headers={'Location': '/api/v3/stolen'})
        code, _, stderr = self.invoke('sonarr', 'command', 'RefreshSeries')
        self.assertEqual(code, 2)
        self.assertIn('write request', stderr)
        self.assertEqual(len(api.Handler.calls), 1)

    def test_traversal_and_foreign_urls_are_rejected(self):
        for path in ('http://other.example/api', '//other.example/api', '/a/../b', '/%252e%252e/secret', '/a\\b'):
            code, _, _ = self.invoke('sonarr', 'api', 'GET', path)
            self.assertEqual(code, 2, path)
        self.assertEqual(api.Handler.calls, [])

    def test_html_is_not_api_success(self):
        self.route('GET', '/api/v3/health', b'<html>login</html>', headers={'Content-Type': 'text/html'})
        code, _, stderr = self.invoke('sonarr', 'health')
        self.assertEqual(code, 2)
        self.assertIn('HTML', stderr)

    def test_binary_export_is_private_and_exclusive(self):
        self.route('GET', '/api/v3/image', b'\xff\x00\x01', headers={'Content-Type': 'image/png'})
        dest = self.directory / 'art.png'
        code, data, _ = self.invoke('sonarr', 'api', 'GET', '/image', '--output', str(dest))
        self.assertEqual((code, data['bytes']), (0, 3))
        self.assertEqual(dest.read_bytes(), b'\xff\x00\x01')
        self.assertEqual(dest.stat().st_mode & 0o777, 0o600)
        code, _, _ = self.invoke('sonarr', 'api', 'GET', '/image', '--output', str(dest))
        self.assertEqual(code, 2)
        self.assertEqual(len(api.Handler.calls), 1)

    def test_journals_and_errors_redact_secrets(self):
        self.route('POST', '/api/v3/command', {'message': api.KEY}, status=400)
        code, _, stderr = self.invoke('sonarr', 'command', 'RefreshSeries', '--set', 'apiKey=' + api.KEY)
        self.assertEqual(code, 2)
        self.assertNotIn(api.KEY, stderr)
        self.assertNotIn(api.KEY, (self.directory / 'requests.jsonl').read_text())

    def test_qbittorrent_forms_and_delete_keep_data(self):
        code, data, _ = self.invoke('qbittorrent', 'torrent-action', 'delete', '--hashes', 'hash', '--dry-run')
        self.assertEqual(code, 0)
        self.assertIn(['deleteFiles', False], data['request']['form'])
        self.route('POST', '/api/v2/torrents/setCategory', b'')
        code, _, _ = self.invoke('qbittorrent', 'torrent-action', 'category', '--hashes', 'abc', '--category', 'tv-sonarr')
        self.assertEqual(code, 0)
        self.assertIn(b'category=tv-sonarr', api.Handler.calls[-1]['body'])

    def test_multipart_preserves_upload_bytes(self):
        self.route('POST', '/api/upload', {'ok': True})
        file = self.directory / 'test.srt'
        file.write_bytes(b'French subtitles\n\x00\xff')
        code, _, _ = self.invoke('bazarr', 'api', 'POST', '/upload', '--file', 'file=' + str(file), '--form', 'language=fr')
        self.assertEqual(code, 0)
        self.assertIn(file.read_bytes(), api.Handler.calls[-1]['body'])

    def test_jellyfin_metadata_preserves_language_and_omits_readonly(self):
        self.route('GET', '/Items/item', {'Id': 'item', 'Name': 'Show', 'OriginalLanguage': 'ja',
            'OriginalTitle': 'Show', 'TrickplayInfo': {}, 'People': [{'Name': 'Person'}]})
        code, data, _ = self.invoke('jellyfin', 'metadata', 'item', '--user-id', 'user', '--set', 'OriginalTitle=New', '--dry-run')
        self.assertEqual(code, 0)
        body = data['request']['body']
        self.assertEqual(body['OriginalLanguage'], 'ja')
        self.assertEqual(body['OriginalTitle'], 'New')
        self.assertNotIn('TrickplayInfo', body)
        self.assertNotIn('People', body)

    def test_openapi_details_and_call_validation(self):
        spec = optimization.DiscoveryTests.schema(self)
        self.route('GET', '/api/v3/openapi.json', spec)
        code, data, _ = self.invoke('sonarr', 'ops', '--search', 'editSeries', '--details')
        self.assertEqual(code, 0)
        self.assertEqual(data['rows'][0]['body']['fields'], {'monitored': 'boolean'})
        code, _, _ = self.invoke('sonarr', 'call', 'editSeries')
        self.assertEqual(code, 2)
        self.assertTrue(all(c['method'] == 'GET' for c in api.Handler.calls))
        self.route('PUT', '/api/v3/series/7', {'id': 7})
        code, data, _ = self.invoke('sonarr', 'call', 'editSeries', '-p', 'id=7', '--data', '{"monitored":true}')
        self.assertEqual((code, data['id']), (0, 7))

    def test_private_config_and_native_argument_errors(self):
        cfg = self.cli_config()
        cfg.chmod(0o644)
        p = subprocess.run([str(BINARY), '--config', str(cfg), 'sonarr', 'status'], capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        self.assertNotIn(api.KEY, p.stderr)
        cfg.chmod(0o600)
        for flags in (['--wait-timeout', '-1'], ['--wait-timeout', '1e100'], ['--poll-interval', '0'], ['--max-output', '0'], ['--timeout', 'nan']):
            code, _, _ = self.invoke('sonarr', 'command', 'RefreshSeries', '--wait', *flags)
            self.assertEqual(code, 2)
        self.assertEqual(api.Handler.calls, [])

    def test_encoded_subsonic_writes_cannot_be_polled(self):
        code, _, stderr = self.invoke('navidrome', 'wait', '--path', '/%64eletePlaylist.view', '--until', 'done=true')
        self.assertEqual(code, 2)
        self.assertIn('read-only', stderr)
        self.assertEqual(api.Handler.calls, [])

    def test_base64_upload_preserves_bytes_and_mime(self):
        import base64
        file = self.directory / 'logo.png'
        file.write_bytes(b'\xff\x00\xff\x01')
        self.route('POST', '/upload', {})
        code, _, stderr = self.invoke('jellyfin', 'api', 'POST', '/upload', '--upload', str(file), '--base64-upload')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(base64.b64decode(api.Handler.calls[-1]['body']), file.read_bytes())
        self.assertEqual(api.Handler.calls[-1]['headers']['content-type'], 'image/png')

    def test_metadata_readback_verifies_requested_fields(self):
        item = {'Id': 'item', 'Name': 'Show', 'OriginalLanguage': 'ja', 'OriginalTitle': 'Show'}
        api.Handler.routes[('GET', '/Items/item')] = lambda call: (200, dict(item))
        def update(call):
            item.update(call['body'])
            return 204, b''
        api.Handler.routes[('POST', '/Items/item')] = update
        code, data, _ = self.invoke('jellyfin', 'metadata', 'item', '--user-id', 'user', '--set', 'OriginalTitle=New')
        self.assertEqual((code, data['changedFields']), (0, ['OriginalTitle']))
        self.assertEqual(item['OriginalLanguage'], 'ja')
        self.assertEqual([c['method'] for c in api.Handler.calls], ['GET', 'POST', 'GET'])

    def test_backdrop_replacement_removes_only_superseded_image(self):
        images = [{'ImageType': 'Backdrop', 'ImageIndex': 0}]
        api.Handler.routes[('GET', '/Items/item/Images')] = lambda call: (200, list(images))
        def download(call):
            images.append({'ImageType': 'Backdrop', 'ImageIndex': 1})
            return 204, b''
        api.Handler.routes[('POST', '/Items/item/RemoteImages/Download')] = download
        self.route('POST', '/Items/item/Images/Backdrop/1/Index', {})
        def remove(call):
            images.pop()
            return 204, b''
        api.Handler.routes[('DELETE', '/Items/item/Images/Backdrop/1')] = remove
        code, data, _ = self.invoke('jellyfin', 'image-set', 'item', '--type', 'Backdrop', '--url', 'https://example.invalid/art.png')
        self.assertEqual(code, 0)
        self.assertEqual(len(data['images']), 1)
        self.assertEqual([c['path'] for c in api.Handler.calls if c['method'] == 'DELETE'], ['/Items/item/Images/Backdrop/1'])

    def test_add_checks_existing_roots_profiles_and_ambiguous_lookup(self):
        self.route('GET', '/api/v3/rootfolder', [{'path': '/media/Films'}])
        self.route('GET', '/api/v3/qualityprofile', [{'id': 1}])
        self.route('GET', '/api/v3/movie/lookup', [{'title': 'A'}, {'title': 'B'}])
        code, _, stderr = self.invoke('radarr', 'add', 'ambiguous', '--root', '/media/Films', '--profile-id', '1')
        self.assertEqual(code, 2)
        self.assertIn('2 candidates', stderr)
        self.assertTrue(all(c['method'] == 'GET' for c in api.Handler.calls))
        self.route('GET', '/api/v3/movie/lookup', [{'title': 'Exact movie', 'tmdbId': 550}])
        code, data, _ = self.invoke('radarr', 'add', 'tmdb:550', '--root', '/media/Films', '--profile-id', '1', '--dry-run', '--full')
        self.assertEqual(code, 0)
        self.assertFalse(data['request']['body']['addOptions']['searchForMovie'])

    def test_discovered_query_arrays_and_site_paths(self):
        spec = {'openapi': '3.0.1', 'paths': {
            '/api/v3/rename': {'get': {'operationId': 'preview', 'parameters': [
                {'name': 'movieId', 'in': 'query', 'schema': {'type': 'array', 'items': {'type': 'integer'}}}]}},
            '/login': {'get': {'operationId': 'siteLogin'}}}}
        self.route('GET', '/api/v3/openapi.json', spec)
        self.route('GET', '/api/v3/rename', [])
        self.route('GET', '/login', {'fixture': True})
        code, _, _ = self.invoke('sonarr', 'call', 'preview', '-q', 'movieId=[1,2]')
        self.assertEqual(code, 0)
        self.assertEqual(api.Handler.calls[-1]['query']['movieId'], ['1', '2'])
        code, data, _ = self.invoke('sonarr', 'call', 'siteLogin')
        self.assertEqual((code, data['fixture']), (0, True))
        self.assertEqual(api.Handler.calls[-1]['path'], '/login')

    def test_pagination_stops_ignored_parameters(self):
        self.route('GET', '/api/v3/queue', {'records': [{'id': 1}], 'totalRecords': 500})
        code, _, stderr = self.invoke('sonarr', 'queue', '--all')
        self.assertEqual(code, 2)
        self.assertIn('repeated a page', stderr)
        self.assertEqual(len(api.Handler.calls), 2)

    def test_offline_output_matches_python_reference(self):
        from autoarr.cli import run
        from autoarr.output import render_result
        cfg = self.cli_config()
        path = self.directory / 'rows.json'
        path.write_text(json.dumps([{'id': i, 'title': f'Show {i}', 'seeders': i % 4, 'ep': [i, i + 1],
                                    'languages': [{'name': 'French' if i % 2 else 'English'}]} for i in range(20)]))
        cases = [[], ['--count'], ['--group-by', 'seeders'], ['--filter', 'id in [1,3,9]'],
                 ['--filter', 'seeders between [1,3]'], ['--filter', 'ep overlaps [2,5]'],
                 ['--filter', 'languages.*.name!=French'], ['--sort=-seeders,id', '--unique-by', 'seeders'],
                 ['--filter', 'title=~"Show (1|2)$"'], ['--full', '--limit', '3'],
                 ['--select', 'id,languages[0].name'], ['--limit', '0']]
        for flags in cases:
            with self.subTest(flags=flags):
                argv = ['--config', str(cfg), '--no-cache', 'result', '--file', str(path), *flags]
                redactor, options, data = run(argv)
                expected = json.loads(render_result(redactor.clean(data), options))
                process = subprocess.run([str(BINARY), *argv], capture_output=True, text=True)
                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertEqual(json.loads(process.stdout), expected)


if __name__ == '__main__':
    unittest.main()
