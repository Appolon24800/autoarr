"""Wait integration tests use a local API and a virtual clock, never live media."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch

from test_cli import Handler, HttpFixture, KEY
from autoarr.cli import main
from autoarr.config import AutoarrError
from autoarr.waiting import wait_for


class Clock:
    def __init__(self):
        self.now = 0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class WaitTests(HttpFixture):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.timer = patch('autoarr.waiting.time', self.clock)
        self.timer.start()
        self.addCleanup(self.timer.stop)

    def invoke(self, kind, *argv):
        config = self.cli_config(kind)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(['--config', str(config), kind, *argv])
        return code, json.loads(stdout.getvalue()) if stdout.getvalue() else None, stderr.getvalue()

    def sequence(self, path, responses):
        values = iter(responses)
        Handler.routes[('GET', path)] = lambda call: (200, next(values))

    def queue(self, rows):
        self.route('GET', '/api/v3/queue', {'records': rows, 'totalRecords': len(rows)})

    def history(self, rows):
        self.route('GET', '/api/v3/history', {'records': rows, 'totalRecords': len(rows)})

    def test_command_wait_streams_progress_and_submits_only_once(self):
        self.route('POST', '/api/v3/command', {'id': 5})
        self.sequence('/api/v3/command/5', [{'id': 5, 'status': 'queued'},
            {'id': 5, 'status': 'started', 'message': 'Scanning'}, {'id': 5, 'status': 'completed'}])
        code, data, stderr = self.invoke('sonarr', 'command', 'RefreshSeries', '--set', 'seriesId=7', '--wait')
        self.assertEqual((code, data['status'], data['polls']), (0, 'completed', 3))
        self.assertEqual(self.clock.sleeps, [5, 5])
        self.assertEqual(len([c for c in Handler.calls if c['method'] == 'POST']), 1)
        events = [json.loads(line) for line in stderr.splitlines()]
        self.assertEqual([e['progress']['status'] for e in events], ['queued', 'started', 'completed'])

    def test_command_timeout_returns_intact_resume_and_caps_sleep(self):
        self.route('GET', '/api/v3/command/5', {'id': 5, 'status': 'started'})
        code, data, _ = self.invoke('sonarr', 'wait', '--command-id', '5', '--wait-timeout', '3', '--poll-interval', '20')
        self.assertEqual((code, data['status'], data['elapsedSeconds']), (3, 'timeout', 3))
        self.assertEqual(self.clock.sleeps, [3])
        self.assertGreater(len(data['resume']), 8)  # Never truncate an argv array.
        self.route('GET', '/api/v3/command/5', {'id': 5, 'status': 'completed'})
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(data['resume'][1:]), 0)
        self.assertTrue(all(c['method'] == 'GET' for c in Handler.calls))

    def test_failure_and_abort_are_nonzero_and_resumable(self):
        for status in ('failed', 'aborted', 'cancelled', 'orphaned'):
            with self.subTest(status=status):
                self.route('GET', '/api/v3/command/5', {'id': 5, 'status': status})
                code, data, _ = self.invoke('sonarr', 'wait', '--command-id', '5')
                self.assertEqual((code, data['status']), (2, 'failed'))
                self.assertIn('--resume', data['resume'])

    def test_dry_run_does_not_poll_or_submit(self):
        code, data, stderr = self.invoke('sonarr', 'command', 'RefreshSeries', '--wait', '--dry-run')
        self.assertEqual(code, 0)
        self.assertTrue(data['dryRun'])
        self.assertEqual(Handler.calls, [])
        self.assertEqual(stderr, '')
        code, data, _ = self.invoke('sonarr', 'wait', '--command-id', '5', '--dry-run')
        self.assertEqual((code, data['dryRun']), (0, True))
        self.assertEqual(Handler.calls, [])

    def test_zero_timeout_returns_target_without_polling(self):
        code, data, _ = self.invoke('sonarr', 'wait', '--command-id', '5', '--wait-timeout', '0')
        self.assertEqual((code, data['status'], data['polls']), (3, 'timeout', 0))
        self.assertEqual(Handler.calls, [])

    def test_invalid_waits_fail_before_writes(self):
        invalid = [('--wait-timeout', '-1'), ('--wait-timeout', 'nan'),
                   ('--poll-interval', '0'), ('--poll-interval', 'inf'),
                   ('--wait-path', '/work'), ('--wait-path', '/work', '--until', 'bad expression')]
        for flags in invalid:
            with self.subTest(flags=flags), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.invoke('sonarr', 'command', 'RefreshSeries', '--wait', *flags)
        self.assertEqual(Handler.calls, [])

    def test_quiet_progress_keeps_stdout_machine_readable(self):
        self.route('GET', '/api/v3/command/5', {'status': 'completed'})
        code, data, stderr = self.invoke('sonarr', 'wait', '--command-id', '5', '--quiet-progress')
        self.assertEqual((code, data['status'], stderr), (0, 'completed', ''))

    def test_progress_is_redacted_and_terminal_controls_are_escaped(self):
        self.route('GET', '/api/v3/command/5', {'status': 'completed', 'message': KEY + '\x1b[31m'})
        _, data, stderr = self.invoke('sonarr', 'wait', '--command-id', '5')
        self.assertNotIn(KEY, stderr + json.dumps(data))
        self.assertNotIn('\x1b', stderr)

    def test_repeated_progress_is_deduplicated_with_heartbeat(self):
        self.route('GET', '/api/v3/command/5', {'status': 'started'})
        code, data, stderr = self.invoke('sonarr', 'wait', '--command-id', '5', '--wait-timeout', '36')
        self.assertEqual((code, data['polls']), (3, 8))
        self.assertEqual(len(stderr.splitlines()), 2)

    def test_temporary_http_errors_retry_reads_only(self):
        values = iter([(503, {'message': 'busy'}), (200, {'status': 'completed'})])
        Handler.routes[('GET', '/api/v3/command/5')] = lambda call: next(values)
        code, data, _ = self.invoke('sonarr', 'wait', '--command-id', '5')
        self.assertEqual((code, data['polls']), (0, 2))
        self.assertTrue(all(c['method'] == 'GET' for c in Handler.calls))

    def test_http_failure_after_submit_preserves_target(self):
        self.route('POST', '/api/v3/command', {'id': 5})
        self.route('GET', '/api/v3/command/5', {'message': 'Not allowed'}, status=403)
        code, data, _ = self.invoke('sonarr', 'command', 'RefreshSeries', '--wait')
        self.assertEqual((code, data['status'], data['target']['id']), (2, 'failed', 5))
        self.assertEqual(len([c for c in Handler.calls if c['method'] == 'POST']), 1)

    def test_interrupt_returns_resume_without_cancelling_job(self):
        self.route('GET', '/api/v3/command/5', {'status': 'started'})
        with patch.object(self.clock, 'sleep', side_effect=KeyboardInterrupt):
            code, data, _ = self.invoke('sonarr', 'wait', '--command-id', '5')
        self.assertEqual((code, data['status']), (130, 'interrupted'))
        self.assertTrue(all(c['method'] == 'GET' for c in Handler.calls))

    def test_poll_requests_use_remaining_timeout_and_restore_client(self):
        client = self.client()
        self.route('GET', '/api/v3/command/5', {'status': 'completed'})
        with patch.object(client.opener, 'open', wraps=client.opener.open) as opened:
            outcome = wait_for(client, {'type': 'command', 'id': 5}, timeout=0.5, quiet=True)
        self.assertEqual(outcome['status'], 'completed')
        self.assertEqual(opened.call_args.kwargs['timeout'], 0.5)
        self.assertEqual(client.timeout, 2)

    def test_download_waits_for_import_instead_of_transfer_completion(self):
        self.sequence('/api/v3/queue', [
            {'records': [{'downloadId': 'ABC', 'status': 'downloading', 'size': 100, 'sizeleft': 50}], 'totalRecords': 1},
            {'records': [{'downloadId': 'ABC', 'status': 'completed', 'trackedDownloadState': 'importPending'}], 'totalRecords': 1},
            {'records': [], 'totalRecords': 0}])
        self.history([{'id': 4, 'downloadId': 'ABC', 'eventType': 'downloadFolderImported'}])
        code, data, stderr = self.invoke('sonarr', 'wait', '--download-id', 'abc')
        self.assertEqual((code, data['polls'], data['progress']['state']), (0, 3, 'imported'))
        self.assertEqual(json.loads(stderr.splitlines()[0])['progress']['percent'], 50)

    def test_downloaded_goal_returns_before_import(self):
        self.queue([{'downloadId': 'ABC', 'status': 'completed', 'trackedDownloadState': 'importPending'}])
        code, data, _ = self.invoke('sonarr', 'wait', '--download-id', 'ABC', '--wait-until', 'downloaded')
        self.assertEqual((code, data['polls']), (0, 1))
        self.assertEqual(len(Handler.calls), 1)

    def test_empty_queue_and_unrelated_import_are_not_success(self):
        self.queue([])
        self.history([{'id': 4, 'downloadId': 'OTHER', 'eventType': 'downloadFolderImported'}])
        code, data, _ = self.invoke('sonarr', 'wait', '--download-id', 'ABC', '--wait-timeout', '1')
        self.assertEqual((code, data['status']), (3, 'timeout'))

    def test_blocked_import_fails_with_native_reason(self):
        self.queue([{'downloadId': 'ABC', 'status': 'completed', 'trackedDownloadState': 'importBlocked',
            'statusMessages': [{'title': 'No matching episodes', 'messages': ['Manual import required']}]}])
        code, data, _ = self.invoke('sonarr', 'wait', '--download-id', 'ABC')
        self.assertEqual((code, data['status']), (2, 'failed'))
        self.assertIn('Manual import required', json.dumps(data))

    def test_download_wait_searches_all_queue_pages(self):
        def page(call):
            rows = [{'downloadId': 'OTHER'}] * 250 if call['query']['page'] == ['1'] else [
                {'downloadId': 'ABC', 'trackedDownloadState': 'imported'}]
            return 200, {'records': rows, 'totalRecords': 251}
        Handler.routes[('GET', '/api/v3/queue')] = page
        code, data, _ = self.invoke('sonarr', 'wait', '--download-id', 'ABC')
        self.assertEqual((code, data['status']), (0, 'completed'))
        self.assertEqual(len(Handler.calls), 2)

    def test_season_pack_requires_every_grabbed_episode_import(self):
        self.queue([])
        self.history([{'id': 1, 'downloadId': 'ABC', 'eventType': 'grabbed', 'episodeId': 11},
            {'id': 2, 'downloadId': 'ABC', 'eventType': 'grabbed', 'episodeId': 12},
            {'id': 3, 'downloadId': 'ABC', 'eventType': 'downloadFolderImported', 'episodeId': 11}])
        code, data, _ = self.invoke('sonarr', 'wait', '--download-id', 'ABC', '--wait-timeout', '1')
        self.assertEqual((code, data['status']), (3, 'timeout'))

    def grab_fixture(self, ambiguous=False):
        self.route('GET', '/api/v3/indexer/2', {'id': 2, 'name': 'Approved indexer'})
        old = {'id': 10, 'sourceTitle': 'Exact Release', 'downloadId': 'OLD',
               'eventType': 'grabbed', 'data': {'indexer': 'Approved indexer'}}
        fresh = {**old, 'id': 11, 'downloadId': 'ABC'}
        def history(call):
            if call['query'].get('pageSize') == ['1']:
                rows = [old]
            else:
                rows = [fresh, {**fresh, 'id': 12, 'downloadId': 'DEF'}, old] if ambiguous else [fresh, old]
            return 200, {'records': rows, 'totalRecords': len(rows)}
        Handler.routes[('GET', '/api/v3/history')] = history
        self.route('POST', '/api/v3/release', {'guid': 'release-guid', 'title': 'Exact Release', 'indexerId': 2})
        self.queue([{'downloadId': 'ABC', 'trackedDownloadState': 'imported'}])
        return json.dumps({'guid': 'release-guid', 'title': 'Exact Release', 'indexerId': 2})

    def test_grab_without_download_id_is_correlated_and_never_repeated(self):
        body = self.grab_fixture()
        code, data, _ = self.invoke('sonarr', 'grab', '--data', body, '--wait')
        self.assertEqual((code, data['target']['type'], data['target']['id']), (0, 'download', 'ABC'))
        self.assertEqual(data['target']['afterHistoryId'], 10)
        self.assertEqual(len([c for c in Handler.calls if c['method'] == 'POST']), 1)

    def test_grab_body_from_stdin_is_only_read_once(self):
        body = self.grab_fixture()
        with patch('sys.stdin', io.StringIO(body)):
            code, data, _ = self.invoke('sonarr', 'grab', '--data', '-', '--wait')
        self.assertEqual((code, data['status']), (0, 'completed'))

    def test_ambiguous_grabs_fail_instead_of_guessing(self):
        body = self.grab_fixture(ambiguous=True)
        code, data, _ = self.invoke('sonarr', 'grab', '--data', body, '--wait')
        self.assertEqual((code, data['status']), (2, 'failed'))
        self.assertIn('ambiguous', data['progress']['state'])

    def test_incomplete_release_tracking_is_rejected_before_grab(self):
        code, _, stderr = self.invoke('sonarr', 'grab', '--guid', 'release-guid', '--indexer-id', '2', '--wait')
        self.assertEqual(code, 2)
        self.assertIn('title', stderr)
        self.assertEqual(Handler.calls, [])

    def test_release_resume_survives_long_titles_without_truncation(self):
        target = {'type': 'release', 'title': 'x' * 220, 'indexer': 'Approved indexer',
                  'afterHistoryId': 10, 'goal': 'imported'}
        code, data, _ = self.invoke('sonarr', 'wait', '--resume', json.dumps(target), '--wait-timeout', '0')
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(data['resume'][data['resume'].index('--resume') + 1]), target)

    def test_small_output_budget_keeps_status_and_readonly_target(self):
        code, data, _ = self.invoke('sonarr', 'wait', '--command-id', '5',
                                    '--wait-timeout', '0', '--max-output', '256')
        self.assertEqual((code, data['status'], data['target']['id']), (3, 'timeout', '5'))
        self.assertTrue(data['outputLimited'])
        self.assertLessEqual(len(json.dumps(data, separators=(',', ':'))), 256)

    def test_oversized_target_remains_available_in_private_snapshot(self):
        target = {'type': 'release', 'title': 'x' * 8000, 'indexer': 'Approved indexer',
                  'afterHistoryId': 10, 'goal': 'imported'}
        code, data, _ = self.invoke('sonarr', 'wait', '--resume', json.dumps(target), '--wait-timeout', '0')
        self.assertEqual((code, data['status']), (3, 'timeout'))
        from autoarr.results import Results
        saved = Results(self.directory).load(data['result'])['data']
        self.assertEqual(saved['target'], target)
        self.assertEqual(json.loads(saved['resume'][saved['resume'].index('--resume') + 1]), target)

    def test_torrent_completion_waits_for_checking_and_moving(self):
        self.sequence('/api/v2/torrents/info', [[{'hash': 'ABC', 'state': s, 'progress': p}]
            for s, p in [('downloading', 0.5), ('checkingUP', 1), ('moving', 1), ('stalledUP', 1)]])
        code, data, _ = self.invoke('qbittorrent', 'wait', '--torrent-hash', 'abc')
        self.assertEqual((code, data['polls']), (0, 4))

    def test_missing_or_failed_torrent_is_not_success(self):
        self.route('GET', '/api/v2/torrents/info', [])
        code, data, _ = self.invoke('qbittorrent', 'wait', '--torrent-hash', 'abc', '--wait-timeout', '1')
        self.assertEqual((code, data['status']), (3, 'timeout'))
        self.route('GET', '/api/v2/torrents/info', [{'hash': 'abc', 'state': 'missingFiles', 'progress': 1}])
        code, data, _ = self.invoke('qbittorrent', 'wait', '--torrent-hash', 'abc')
        self.assertEqual((code, data['status']), (2, 'failed'))

    def test_task_run_ignores_previous_success_and_waits_for_new_execution(self):
        old = {'StartTimeUtc': 'yesterday', 'Status': 'Completed'}
        new = {'StartTimeUtc': 'today', 'Status': 'Completed'}
        self.sequence('/ScheduledTasks/scan', [
            {'State': 'Idle', 'LastExecutionResult': old},  # Before starting.
            {'State': 'Idle', 'LastExecutionResult': old},  # Accepted but not started.
            {'State': 'Running', 'CurrentProgressPercentage': 25, 'LastExecutionResult': old},
            {'State': 'Idle', 'LastExecutionResult': new}])
        self.route('POST', '/ScheduledTasks/Running/scan', {})
        code, data, _ = self.invoke('jellyfin', 'task-run', 'scan', '--wait')
        self.assertEqual((code, data['polls']), (0, 3))
        self.assertEqual(len([c for c in Handler.calls if c['method'] == 'POST']), 1)

    def test_task_failed_execution_is_nonzero(self):
        self.route('GET', '/ScheduledTasks/scan', {'State': 'Idle', 'LastExecutionResult': {'Status': 'Failed'}})
        code, data, _ = self.invoke('jellyfin', 'wait', '--task-id', 'scan')
        self.assertEqual((code, data['status']), (2, 'failed'))

    def test_following_running_task_preserves_execution_baseline_on_resume(self):
        old = {'StartTimeUtc': 'yesterday', 'Status': 'Completed'}
        self.route('GET', '/ScheduledTasks/scan', {'State': 'Running', 'LastExecutionResult': old})
        code, data, _ = self.invoke('jellyfin', 'wait', '--task-id', 'scan', '--wait-timeout', '1')
        self.assertEqual((code, data['target']['baseline']), (3, old))
        self.route('GET', '/ScheduledTasks/scan', {'State': 'Idle', 'LastExecutionResult': old})
        code, data, _ = self.invoke('jellyfin', 'wait', '--resume', json.dumps(data['target']), '--wait-timeout', '1')
        self.assertEqual((code, data['status']), (3, 'timeout'))

    def test_history_from_previous_grab_of_same_hash_is_not_completion(self):
        self.queue([])
        self.history([{'id': 9, 'downloadId': 'ABC', 'eventType': 'downloadFolderImported'}])
        target = {'type': 'download', 'id': 'ABC', 'afterHistoryId': 10, 'goal': 'imported'}
        code, data, _ = self.invoke('sonarr', 'wait', '--resume', json.dumps(target), '--wait-timeout', '1')
        self.assertEqual((code, data['status']), (3, 'timeout'))

    def test_sleep_chunks_preserve_long_poll_interval(self):
        self.route('GET', '/api/v3/command/5', {'status': 'started'})
        code, data, _ = self.invoke('sonarr', 'wait', '--command-id', '5',
                                    '--wait-timeout', '121', '--poll-interval', '120')
        self.assertEqual((code, data['polls']), (3, 2))
        self.assertEqual(self.clock.sleeps, [60, 60, 1])

    def test_generic_condition_waits_after_one_write_and_failure_wins(self):
        self.route('POST', '/work', {'accepted': True})
        self.sequence('/work/1', [{'state': 'running'}, {'state': 'done', 'error': True}])
        code, data, _ = self.invoke('jellyfin', 'api', 'POST', '/work', '--wait',
            '--wait-path', '/work/1', '--until', 'state=done', '--fail-if', 'error=true')
        self.assertEqual((code, data['status'], data['polls']), (2, 'failed', 2))
        self.assertEqual(len([c for c in Handler.calls if c['method'] == 'POST']), 1)

    def test_generic_conditions_use_raw_response_not_display_selection(self):
        self.sequence('/work', [[], [{'state': 'done'}, {'state': 'running'}], [{'state': 'done'}]])
        code, data, _ = self.invoke('jellyfin', 'wait', '--path', '/work', '-q', 'id=7',
            '--until', 'state=done', '--select', 'status,polls')
        self.assertEqual((code, data['status'], data['polls']), (0, 'completed', 3))
        self.assertEqual(Handler.calls[0]['query']['id'], ['7'])

    def test_generic_envelope_conditions_can_wait_for_empty_queue(self):
        self.queue([])
        code, data, _ = self.invoke('sonarr', 'wait', '--path', '/queue', '--until', 'totalRecords=0')
        self.assertEqual((code, data['status']), (0, 'completed'))

    def test_raw_action_with_unverifiable_wait_is_rejected_before_write(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.invoke('jellyfin', 'refresh', 'item', '--wait')
        self.assertEqual(Handler.calls, [])

    def test_oversized_uncached_target_is_rejected_before_write(self):
        code, _, stderr = self.invoke('jellyfin', 'api', 'POST', '/work', '--wait',
            '--wait-path', '/work/1', '--until', 'name=' + 'x' * 300, '--max-output', '256', '--no-cache')
        self.assertEqual(code, 2)
        self.assertIn('before submitting', stderr)
        self.assertEqual(Handler.calls, [])

    def test_action_help_explains_wait_and_condition_options(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as exc:
            main(['sonarr', 'wait', '--help'])
        self.assertEqual(exc.exception.code, 0)
        self.assertIn('--wait-timeout', out.getvalue())
        self.assertIn('--until', out.getvalue())

    def test_resume_cannot_forward_credentials_to_foreign_url(self):
        target = {'type': 'condition', 'path': 'https://other.example/api', 'until': ['done=true']}
        code, _, stderr = self.invoke('sonarr', 'wait', '--resume', json.dumps(target))
        self.assertEqual(code, 2)
        self.assertIn('relative', stderr)
        self.assertEqual(Handler.calls, [])

    def test_resume_cannot_poll_a_subsonic_mutation(self):
        with self.assertRaisesRegex(AutoarrError, 'read-only'):
            wait_for(self.client('navidrome'), {'type': 'condition', 'path': '/deletePlaylist.view', 'until': ['done=true']})

    def test_batch_stops_followup_write_after_wait_timeout(self):
        self.route('GET', '/api/v3/command/5', {'status': 'started'})
        config = self.cli_config()
        jobs = [['sonarr', 'wait', '--command-id', '5', '--wait-timeout', '1'],
                ['sonarr', 'command', 'RefreshSeries']]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(['--config', str(config), 'batch', '--file', json.dumps(jobs)])
        data = json.loads(out.getvalue())
        self.assertEqual((code, data['failed'], data['skipped']), (1, 1, 1))
        self.assertEqual(data['jobs'][0]['exitCode'], 3)
        self.assertTrue(all(c['method'] == 'GET' for c in Handler.calls))


if __name__ == '__main__':
    unittest.main()
