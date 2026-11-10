"""Regression coverage for fewer tool round trips and faster batch execution."""
import contextlib
import io
import json
import threading
import unittest
from unittest.mock import patch

from test_cli import Handler, HttpFixture
from autoarr.catalog import Catalog
from autoarr.cli import main, parse
from autoarr.config import AutoarrError, read_config


class OptimizationTests(HttpFixture):
    def invoke(self, jobs, *flags):
        cfg = self.cli_config()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--config', str(cfg), 'batch', *flags, '--file', json.dumps(jobs)])
        return code, json.loads(out.getvalue()) if out.getvalue() else None, err.getvalue()

    def source(self, value):
        self.route('GET', '/api/v3/series/7', value)

    def test_static_batch_parses_each_child_once_and_reads_config_once(self):
        self.source({'id': 7})
        with patch('autoarr.cli.parse', wraps=parse) as parsing, patch('autoarr.cli.read_config', wraps=read_config) as reading:
            code, data, _ = self.invoke([['sonarr', 'get', 'series', '7']] * 3)
        self.assertEqual((code, data['succeeded']), (0, 3))
        self.assertEqual(parsing.call_count, 4)  # Parent + one preflight per child.
        self.assertEqual(reading.call_count, 1)
        self.assertEqual(len(Handler.calls), 3)

    def test_named_step_uses_native_data_hidden_by_display_selection(self):
        self.source({'id': 7, 'title': 'Show', 'monitored': True})
        self.route('GET', '/api/v3/rename', [])
        code, data, _ = self.invoke([
            {'name': 'show', 'argv': ['sonarr', 'get', 'series', '7', '--select', 'title'], 'expect': ['data.monitored=true']},
            ['sonarr', 'rename', '{steps.show.data.id}']])
        # Rename preview is native GET; this test never applies a rename.
        self.assertEqual((code, data['succeeded']), (0, 2))
        self.assertEqual(data['jobs'][0]['name'], 'show')
        self.assertEqual(Handler.calls[-1]['path'], '/api/v3/rename')
        self.assertEqual(Handler.calls[-1]['query']['seriesId'], ['7'])

    def test_named_results_flow_into_offline_replay_without_another_api_call(self):
        self.source([{'id': 1}, {'id': 2}])
        code, data, err = self.invoke([
            {'name': 'shows', 'argv': ['sonarr', 'get', 'series', '7']},
            ['result', '--id', '{steps.shows.result}', '--filter', 'id=2', '--count']])
        self.assertEqual((code, data['succeeded']), (0, 2), err)
        self.assertEqual(data['jobs'][1]['data']['matched'], 1)
        self.assertEqual(len(Handler.calls), 1)

    def test_raw_references_work_without_caching(self):
        self.source({'id': 7})
        code, data, err = self.invoke([
            {'name': 'show', 'argv': ['sonarr', 'get', 'series', '7']},
            ['sonarr', 'get', 'series', '{steps.show.data.id}']], '--no-cache')
        self.assertEqual((code, data['succeeded']), (0, 2), err)
        self.assertFalse((self.directory / 'results').exists())

    def test_json_references_preserve_types_escaping_and_null(self):
        value = {'enabled': True, 'ids': [1, 2], 'optional': None, 'name': 'quote " and $(not-a-command)'}
        self.source(value)
        body = json.dumps({key: '{steps.source.data.' + key + '}' for key in value})
        with patch('subprocess.run', side_effect=AssertionError('Batch must not execute shell commands')):
            code, data, err = self.invoke([
                {'name': 'source', 'argv': ['sonarr', 'get', 'series', '7']},
                ['sonarr', 'api', 'POST', '/custom', '--data', body]], '--dry-run')
        self.assertEqual((code, data['succeeded']), (0, 2), err)
        self.assertEqual(data['jobs'][1]['data']['request']['body'], value)
        self.assertEqual(len(Handler.calls), 1)

    def test_entire_response_can_be_a_json_body(self):
        value = {'id': 7, 'monitored': True}
        self.source(value)
        code, data, err = self.invoke([
            {'name': 'source', 'argv': ['sonarr', 'get', 'series', '7']},
            ['sonarr', 'api', 'POST', '/custom', '--data', '{steps.source.data}']], '--dry-run')
        self.assertEqual(code, 0, err)
        self.assertEqual(data['jobs'][1]['data']['request']['body'], value)

    def test_expect_count_prevents_followup_on_ambiguous_result(self):
        self.source([{'id': 7}, {'id': 8}])
        code, data, _ = self.invoke([
            {'name': 'source', 'argv': ['sonarr', 'get', 'series', '7'], 'expect': ['count=1']},
            ['sonarr', 'command', 'RefreshSeries', '--set', 'seriesId={steps.source.data.0.id}']])
        self.assertEqual((code, data['failed'], data['skipped']), (1, 1, 1))
        self.assertIn('count=1', data['jobs'][0]['error'])
        self.assertTrue(all(c['method'] == 'GET' for c in Handler.calls))

    def test_expect_count_checks_raw_records_before_limits(self):
        self.source([{'id': 7}, {'id': 8}])
        code, data, _ = self.invoke([
            {'argv': ['sonarr', 'get', 'series', '7', '--limit', '1', '--filter', 'id=7'], 'expect': ['count=1']}])
        self.assertEqual((code, data['failed']), (1, 1))

    def test_empty_results_fail_guard_before_index_reference(self):
        self.source([])
        code, data, _ = self.invoke([
            {'name': 'source', 'argv': ['sonarr', 'get', 'series', '7'], 'expect': ['count=1']},
            ['sonarr', 'command', 'RefreshSeries', '--set', 'seriesId={steps.source.data.0.id}']])
        self.assertEqual((code, data['failed'], data['skipped']), (1, 1, 1))
        self.assertEqual(len(Handler.calls), 1)

    def test_failed_dependencies_are_skipped_even_with_keep_going(self):
        self.source({'id': 7})
        code, data, _ = self.invoke([
            {'name': 'first', 'argv': ['sonarr', 'get', 'series', '7'], 'expect': ['data.id=8']},
            {'name': 'second', 'argv': ['sonarr', 'get', 'series', '{steps.first.data.id}']},
            ['sonarr', 'command', 'RefreshSeries', '--set', 'seriesId={steps.second.data.id}'],
            ['sonarr', 'get', 'series', '7']], '--keep-going')
        self.assertEqual((code, data['succeeded'], data['failed'], data['skipped']), (1, 1, 1, 2))
        self.assertEqual(len(Handler.calls), 2)

    def test_missing_response_field_fails_without_followup_write(self):
        self.source({'title': 'no id'})
        code, data, _ = self.invoke([
            {'name': 'show', 'argv': ['sonarr', 'get', 'series', '7']},
            ['sonarr', 'rename', '{steps.show.data.id}', '--apply']])
        self.assertEqual((code, data['failed']), (1, 1))
        self.assertIn('Step reference has no value', data['jobs'][1]['error'])
        self.assertEqual(len(Handler.calls), 1)

    def test_response_cannot_become_a_flag_or_file_input(self):
        for value in ('--delete-files', '-', '@local-secret.json'):
            for option in (['--data', '{steps.source.data.value}'], ['--data={steps.source.data.value}']):
                with self.subTest(value=value, option=option):
                    Handler.calls = []
                    self.source({'value': value})
                    code, data, _ = self.invoke([
                        {'name': 'source', 'argv': ['sonarr', 'get', 'series', '7']},
                        ['sonarr', 'api', 'POST', '/custom', *option]])
                    self.assertEqual((code, data['failed']), (1, 1))
                    self.assertIn('cannot introduce', data['jobs'][1]['error'])
                    self.assertEqual(len(Handler.calls), 1)

    def test_resolved_numeric_ids_are_checked_before_action(self):
        self.source({'id': 'not-a-number'})
        code, data, _ = self.invoke([
            {'name': 'source', 'argv': ['sonarr', 'get', 'series', '7']},
            ['sonarr', 'rename', '{steps.source.data.id}', '--apply']])
        self.assertEqual((code, data['failed']), (1, 1))
        self.assertIn('not started', data['jobs'][1]['error'])
        self.assertEqual(len(Handler.calls), 1)

    def test_forward_refs_duplicate_names_and_invalid_guards_fail_preflight(self):
        invalid = [
            [{'argv': ['sonarr', 'get', 'series', '{steps.later.data.id}']}],
            [{'name': 'same', 'argv': ['sonarr', 'status']}, {'name': 'same', 'argv': ['sonarr', 'status']}],
            [{'name': 'has-hyphens', 'argv': ['sonarr', 'status']}],
            [{'argv': ['sonarr', 'status'], 'expect': ['not a predicate']}],
            [{'argv': ['sonarr', 'status'], 'expect': 'count=1'}],
            [{'argv': ['sonarr', 'get', 'series', 7]}],
            [{'name': 'source', 'argv': ['sonarr', 'status']}, {'argv': ['sonarr', 'get', 'series', '{steps.source.invalid}']}]
        ]
        for jobs in invalid:
            with self.subTest(jobs=jobs):
                code, _, _ = self.invoke([['sonarr', 'command', 'RefreshSeries'], *jobs])
                self.assertEqual(code, 2)
                self.assertEqual(Handler.calls, [])

    def test_uncached_result_reference_is_rejected_before_writes(self):
        code, _, err = self.invoke([
            {'name': 'source', 'argv': ['sonarr', 'command', 'RefreshSeries']},
            ['result', '--id', '{steps.source.result}']], '--no-cache')
        self.assertEqual(code, 2)
        self.assertIn('cached source', err)
        self.assertEqual(Handler.calls, [])

    def test_foreach_and_step_references_compose(self):
        self.source({'seriesId': 7})
        self.route('GET', '/api/v3/episode', [])
        code, data, err = self.invoke([
            {'name': 'show', 'argv': ['sonarr', 'get', 'series', '7']},
            {'name': 'season_{item}', 'foreach': [1, 2],
             'argv': ['sonarr', 'list', 'episode', '-q', 'seriesId={steps.show.data.seriesId}', '-q', 'seasonNumber={item}']}])
        self.assertEqual((code, data['succeeded']), (0, 3), err)
        self.assertEqual([c['query']['seasonNumber'] for c in Handler.calls[1:]], [['1'], ['2']])

    def test_independent_reads_run_together_before_sequential_write(self):
        barrier = threading.Barrier(2)
        finished, wrote = set(), []
        lock = threading.Lock()
        def read(call):
            barrier.wait(timeout=3)
            with lock:
                finished.add(call['path'])
            return 200, {'id': 7}
        Handler.routes[('GET', '/api/v3/series/7')] = read
        Handler.routes[('GET', '/api/v3/series/8')] = read
        def write(call):
            self.assertEqual(len(finished), 2)
            wrote.append(True)
            return 200, {'id': 1}
        Handler.routes[('POST', '/api/v3/command')] = write
        def after(call):
            self.assertTrue(wrote)
            return 200, {'version': 'fixture'}
        Handler.routes[('GET', '/api/v3/system/status')] = after
        code, data, err = self.invoke([
            ['sonarr', 'get', 'series', '7'], ['sonarr', 'get', 'series', '8'],
            ['sonarr', 'command', 'RefreshSeries'], ['sonarr', 'status']])
        self.assertEqual((code, data['succeeded']), (0, 4), err)
        self.assertEqual(data['execution'], 'parallel reads with sequential writes')

    def test_wait_failure_prevents_dependent_followup_with_keep_going(self):
        self.route('GET', '/api/v3/command/7', {'status': 'failed'})
        code, data, _ = self.invoke([
            {'name': 'ready', 'argv': ['sonarr', 'wait', '--command-id', '7', '--quiet-progress']},
            ['sonarr', 'command', 'RefreshSeries', '--set', 'seriesId={steps.ready.data.response.id}']], '--keep-going')
        self.assertEqual((code, data['failed'], data['skipped']), (1, 1, 1))
        self.assertTrue(all(c['method'] == 'GET' for c in Handler.calls))

    def test_failed_expectation_never_cancels_already_completed_native_write(self):
        self.route('POST', '/api/v3/command', {'id': 7})
        code, data, _ = self.invoke([
            {'argv': ['sonarr', 'command', 'RefreshSeries'], 'expect': ['data.id=8']}])
        self.assertEqual((code, data['failed']), (1, 1))
        self.assertEqual(len(Handler.calls), 1)
        self.assertEqual(Handler.calls[0]['method'], 'POST')


class DiscoveryTests(HttpFixture):
    def schema(self):
        return {'openapi': '3.0.1', 'paths': {'/api/v3/series/{id}': {'put': {
            'operationId': 'editSeries', 'parameters': [
                {'name': 'id', 'in': 'path', 'required': True, 'schema': {'type': 'integer'}}],
            'requestBody': {'required': True, 'content': {'application/json': {'schema': {
                'type': 'object', 'properties': {'monitored': {'type': 'boolean'}}}}}},
            'responses': {'200': {'description': 'ok'}}}}}}

    def test_operation_details_include_parameters_and_body_in_one_response(self):
        self.route('GET', '/api/v3/openapi.json', self.schema())
        cfg = self.cli_config()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(['--config', str(cfg), 'sonarr', 'ops', '--search', 'editSeries', '--details'])
        self.assertEqual(code, 0)
        row = json.loads(out.getvalue())['rows'][0]
        self.assertEqual(row['parameters'][0]['name'], 'id')
        self.assertEqual(row['body']['fields'], {'monitored': 'boolean'})
        self.assertEqual(len(Handler.calls), 1)

    def test_invoke_does_not_resolve_unused_response_schema(self):
        spec = self.schema()
        spec['paths']['/api/v3/series/{id}']['put']['responses'] = {'200': {'$ref': '#/not-needed-to-send-request'}}
        self.route('GET', '/api/v3/openapi.json', spec)
        self.route('PUT', '/api/v3/series/7', {'id': 7})
        result = Catalog(self.client()).invoke('editSeries', {'id': 7}, [], body={'monitored': True})
        self.assertEqual(result['id'], 7)

    def test_details_include_choice_values_and_required_body_fields(self):
        spec = self.schema()
        operation = spec['paths']['/api/v3/series/{id}']['put']
        operation['parameters'].append({'name': 'mode', 'in': 'query',
            'schema': {'type': 'string', 'enum': ['fast', 'full']}})
        body = operation['requestBody']['content']['application/json']['schema']
        body['required'] = ['monitored']
        body['properties']['mode'] = {'type': 'string', 'enum': ['fast', 'full']}
        self.route('GET', '/api/v3/openapi.json', spec)
        detail = Catalog(self.client()).operations(details=True)[0]
        self.assertEqual(detail['parameters'][1]['choices'], ['fast', 'full'])
        self.assertEqual(detail['body']['requiredFields'], ['monitored'])
        self.assertEqual(detail['body']['choices']['mode'], ['fast', 'full'])

    def test_full_schema_keeps_response_details_and_index_is_not_mutated(self):
        self.route('GET', '/api/v3/openapi.json', self.schema())
        catalog = Catalog(self.client())
        full = catalog.operation('editSeries')
        self.assertIn('responses', full)
        self.assertNotIn('responses', catalog.operations()[0])
        self.assertNotIn('responses', catalog.operation('editSeries', include_responses=False))

    def test_refresh_rebuilds_operation_index(self):
        self.route('GET', '/api/v3/openapi.json', self.schema())
        catalog = Catalog(self.client())
        self.assertEqual(catalog.operation('editSeries')['method'], 'PUT')
        changed = self.schema()
        changed['paths']['/api/v3/series/{id}']['put']['operationId'] = 'changedId'
        self.route('GET', '/api/v3/openapi.json', changed)
        catalog.operations(refresh=True)
        self.assertEqual(catalog.operation('changedId')['method'], 'PUT')
        with self.assertRaises(AutoarrError):
            catalog.operation('editSeries')


if __name__ == '__main__':
    unittest.main()
