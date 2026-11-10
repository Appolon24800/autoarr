"""Regression coverage for compact output, replay, and repeatable workflows."""
import contextlib
import io
import json
import os
import sys
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from test_cli import HttpFixture, Handler
from autoarr.cli import main, parse, run
from autoarr.config import AutoarrError, private_json
from autoarr.output import filter_rows, get_path, prepare_result, render_result, sort_rows, unique_rows
from autoarr.results import Results
from autoarr.batch import substitute
from autoarr.catalog import Catalog
from autoarr.query import server_filters


def options(**kw):
    return Namespace(full=False, pretty=False, max_output=None, table=False, select=None,
        filter=[], any_filter=[], sort=None, unique_by=None, count=False, group_by=None,
        limit=None, action='list', **kw)


class OutputTests(unittest.TestCase):
    def test_array_wildcards_and_bracket_paths(self):
        row = {'languages': [{'name': 'English'}, {'name': 'French'}]}
        self.assertEqual(get_path(row, 'languages.*.name'), ['English', 'French'])
        self.assertEqual(get_path(row, 'languages[].name'), ['English', 'French'])
        self.assertEqual(get_path(row, 'languages[1].name'), 'French')

    def test_array_filters_or_exclusions_ranges_and_missing(self):
        rows = [{'id': 1, 'ep': [6], 'title': 'Show VOSTFR', 'seeders': 12, 'language': [{'name': 'English'}]},
                {'id': 2, 'ep': [7, 8], 'title': 'Show SUBFRENCH', 'seeders': 4},
                {'id': 3, 'ep': [5], 'title': 'Show MULTI', 'seeders': 0}]
        self.assertEqual([r['id'] for r in filter_rows(rows, ['ep overlaps [6,7,8]', 'seeders>=3'], ['title~=VOSTFR', 'title~=SUBFRENCH'])], [1, 2])
        self.assertEqual([r['id'] for r in filter_rows(rows, ['ep contains 7'])], [2])
        self.assertEqual([r['id'] for r in filter_rows(rows, ['seeders between [3,5]'])], [2])
        self.assertEqual([r['id'] for r in filter_rows(rows, ['title !~=MULTI'])], [1, 2])
        self.assertEqual([r['id'] for r in filter_rows(rows, ['id notin [1,3]'])], [2])
        self.assertEqual([r['id'] for r in filter_rows(rows, ['language.*.name=English'])], [1])
        self.assertEqual([r['id'] for r in filter_rows(rows, ['language missing'])], [2, 3])
        self.assertEqual([r['id'] for r in filter_rows(rows, ['title =~ "VOSTFR|SUBFRENCH"'])], [1, 2])

    def test_invalid_filters_fail_without_evaluation(self):
        for expression in ('bad expression', 'x in 2', 'x between [1]', 'x =~ "["'):
            with self.assertRaises(AutoarrError): filter_rows([], [expression])

    def test_sort_and_dedup_keep_best_seeded_and_keep_unknown_identifiers(self):
        rows = [{'hash': 'a', 'seeders': 2}, {'hash': 'a', 'seeders': 10}, {'hash': None, 'seeders': None}, {'seeders': 0}]
        result = unique_rows(sort_rows(rows, '-seeders'), 'hash')
        self.assertEqual([r['seeders'] for r in result], [10, 0, None])

    def test_default_output_has_counts_and_bounded_fields_rows(self):
        rows = [{'id': i, 'title': f'Show {i}', 'capabilities': ['large'] * 100} for i in range(100)]
        result = json.loads(render_result(rows, options(), 'abcdef123456'))
        self.assertEqual((result['fetched'], result['matched'], result['shown'], result['more']), (100, 100, 10, 90))
        self.assertNotIn('capabilities', result['rows'][0])
        self.assertEqual(result['rows'][2]['ref'], 'abcdef123456:2')
        self.assertEqual(len(rows[0]['capabilities']), 100)

    def test_reference_indices_survive_sort_filter_and_limit(self):
        rows = [{'id': 1, 'seeders': 1}, {'id': 2, 'seeders': 20}, {'id': 3, 'seeders': 10}]
        args = options()
        args.filter, args.sort, args.limit = ['seeders>=5'], '-seeders', 1
        report = json.loads(render_result(rows, args, 'abcdef123456'))
        self.assertEqual(report['rows'][0]['ref'], 'abcdef123456:1')

    def test_full_retains_native_shape_fields_and_values(self):
        data = {'records': [{'id': 1, 'code': 'native', 'score': 9, 'size': 3, 'nested': {'x': list(range(50))}}], 'totalRecords': 1}
        args = options()
        args.full = True
        self.assertEqual(json.loads(render_result(data, args, 'abcdef123456')), data)

    def test_output_budget_is_valid_json_with_explicit_omission(self):
        args = options()
        args.max_output = 600
        text = render_result([{'title': 'x' * 1000, 'id': i} for i in range(100)], args, 'abcdef123456')
        report = json.loads(text)
        self.assertLessEqual(len(text), 600)
        self.assertTrue(report['outputLimited'])
        self.assertLess(report['shown'], report['matched'])

    def test_count_and_group_operate_before_display_limit(self):
        rows = [{'status': 'warning'}] * 20 + [{'status': 'ok'}] * 5
        args = options()
        args.limit, args.count = 1, True
        self.assertEqual(prepare_result(rows, args)['matched'], 25)
        args.count, args.group_by, args.limit = False, 'status', None
        groups = prepare_result(rows, args)['groups']
        self.assertEqual([g['count'] for g in groups], [20, 5])

    def test_queue_issues_are_deduplicated(self):
        rows = [{'id': 1, 'title': 'Album', 'trackedDownloadStatus': 'warning',
                 'statusMessages': [{'title': 'Track', 'messages': ['Wrong edition']} for i in range(500)]}]
        report = prepare_result(rows, options())
        self.assertEqual(report['rows'][0]['issues'], ['Wrong edition'])
        self.assertNotIn('statusMessages', report['rows'][0])

    def test_schema_brief_avoids_response_model_expansion(self):
        data = {'operationId': 'edit', 'method': 'PUT', 'path': '/resource', 'parameters': [],
                'requestBody': {'content': {'application/json': {'schema': {'type': 'object', 'properties': {'name': {'type': 'string'}}}}}},
                'responses': {'200': {'model': ['irrelevant'] * 10000}}}
        args = options()
        args.action = 'schema'
        text = render_result(data, args)
        self.assertLess(len(text), 500)
        self.assertNotIn('irrelevant', text)

    def test_batch_substitution_does_not_evaluate_shell_or_json_braces(self):
        result = substitute(['--data', '{"monitored":true}', '--set', 'Name={item.name}'], {'item.name': '$(touch /tmp/nope)'})
        self.assertEqual(result[1], '{"monitored":true}')
        self.assertEqual(result[-1], 'Name=$(touch /tmp/nope)')

    def test_batch_json_substitution_preserves_types_and_escaping(self):
        result = substitute(['{"name":"{item.name}","enabled":"{item.enabled}","id":"{item.id}"}'],
                            {'item.name': 'A "quoted" title', 'item.enabled': False, 'item.id': 7})
        self.assertEqual(json.loads(result[0]), {'name': 'A "quoted" title', 'enabled': False, 'id': 7})

    def test_batch_budget_includes_omission_metadata(self):
        for budget in (256, 400, 600):
            args = options()
            args.max_output = budget
            args.action = 'batch'
            output = render_result({'total': 20, 'jobs': [{'job': i, 'title': 'x' * 120} for i in range(20)]}, args)
            self.assertLessEqual(len(output), budget)
            self.assertTrue(json.loads(output)['outputLimited'])


class WorkflowTests(HttpFixture):
    # Inherits the existing local HTTP fixtures and their transport checks.
    def invoke(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, json.loads(out.getvalue()) if out.getvalue() else None, err.getvalue()

    def test_cached_release_refs_are_private_and_bound_to_source_app(self):
        store = Results(self.directory)
        key = store.save([{'guid': 'private-guid', 'indexerId': 2}], 'sonarr', 'releases')
        self.assertEqual(store.reference(key + ':0', 'sonarr')['guid'], 'private-guid')
        self.assertEqual((store.directory / (key + '.json')).stat().st_mode & 0o777, 0o600)
        with self.assertRaises(AutoarrError): store.reference(key + ':0', 'radarr')
        with self.assertRaises(AutoarrError): store.reference(key + ':-1', 'sonarr')
        with self.assertRaises(AutoarrError): store.load('../outside')
        os.utime(store.directory / (key + '.json'), (time.time() - 90000,) * 2)
        with self.assertRaisesRegex(AutoarrError, 'expired'): store.load(key)

    def test_result_replay_never_calls_the_server(self):
        cfg = self.cli_config()
        key = Results(cfg.parent).save([{'id': 1}, {'id': 2}], 'sonarr', 'list')
        with patch('autoarr.client.Client.request', side_effect=AssertionError('replay must be offline')):
            code, result, err = self.invoke(['--config', str(cfg), 'result', '--id', key, '--filter', 'id=2'])
        self.assertEqual(code, 0, err)
        self.assertEqual(result['matched'], 1)
        self.assertIn('cachedAt', result)
        self.assertEqual(result['rows'][0]['id'], 2)

    def test_grab_by_reference_sends_original_payload_without_printing_it(self):
        cfg = self.cli_config()
        key = Results(cfg.parent).save([{'guid': 'private-guid', 'indexerId': 2, 'episodeNumbers': [6]}], 'sonarr', 'releases')
        self.route('POST', '/api/v3/release', b'', 204)
        code, result, err = self.invoke(['--config', str(cfg), 'sonarr', 'grab', '--ref', key + ':0'])
        self.assertEqual(code, 0, err)
        self.assertEqual(Handler.calls[-1]['body']['guid'], 'private-guid')

    def test_batch_reads_across_multiple_apps_and_ids(self):
        cfg = self.cli_config()
        self.route('GET', '/api/v3/series/7', {'id': 7, 'title': 'A'})
        self.route('GET', '/api/v3/series/8', {'id': 8, 'title': 'B'})
        code, result, err = self.invoke(['--config', str(cfg), 'batch', '--ids', '7-8', '--', 'sonarr', 'get', 'series', '{id}'])
        self.assertEqual(code, 0, err)
        self.assertEqual(result['succeeded'], 2)
        self.assertEqual(result['execution'], 'parallel reads')
        self.assertEqual(len(Handler.calls), 2)

    def test_parent_dry_run_and_no_cache_propagate_to_every_job(self):
        cfg = self.cli_config()
        self.route('GET', '/api/v3/series/7', {'id': 7, 'monitored': False})
        self.route('GET', '/api/v3/series/8', {'id': 8, 'monitored': False})
        code, result, err = self.invoke(['--config', str(cfg), '--dry-run', '--no-cache', 'batch', '--ids', '7,8', '--',
                                        'sonarr', 'update', 'series', '{id}', '--set', 'monitored=true'])
        self.assertEqual(code, 0, err)
        self.assertEqual(result['succeeded'], 2)
        self.assertTrue(all(c['method'] == 'GET' for c in Handler.calls))
        self.assertFalse((cfg.parent / 'results').exists())

    def test_batch_stops_mutations_after_error_and_reports_skipped_jobs(self):
        cfg = self.cli_config()
        self.route('GET', '/api/v3/series/7', {'id': 7, 'monitored': False})
        self.route('GET', '/api/v3/series/8', {'id': 8, 'monitored': False})
        self.route('PUT', '/api/v3/series/7', {'message': 'fixture failure'}, 400)
        code, result, err = self.invoke(['--config', str(cfg), 'batch', '--ids', '7,8', '--',
                                        'sonarr', 'update', 'series', '{id}', '--set', 'monitored=true'])
        self.assertEqual(code, 1, err)
        self.assertEqual((result['failed'], result['skipped']), (1, 1))
        self.assertFalse(any(c['path'].endswith('/8') for c in Handler.calls))

    def test_invalid_batch_connections_fail_before_any_writes(self):
        cfg = self.cli_config()
        file = self.directory / 'tasks.json'
        file.write_text(json.dumps([['sonarr', 'command', 'RefreshSeries'], ['radarr', 'status']]))
        code, result, err = self.invoke(['--config', str(cfg), 'batch', '--file', '@' + str(file)])
        self.assertEqual(code, 2)
        self.assertEqual(Handler.calls, [])

    def test_json_foreach_is_one_task_and_preserves_typed_values(self):
        cfg = self.cli_config()
        file = self.directory / 'tasks.json'
        file.write_text(json.dumps([{'foreach': [{'id': 7, 'name': 'Title A'}, {'id': 8, 'name': 'Title B'}],
            'argv': ['sonarr', 'api', 'PUT', '/series/{item.id}', '--data', '{"title":"{item.name}"}']}]))
        code, result, err = self.invoke(['--config', str(cfg), '--dry-run', 'batch', '--file', '@' + str(file)])
        self.assertEqual(code, 0, err)
        self.assertEqual(result['succeeded'], 2)
        self.assertEqual(Handler.calls, [])
        self.assertEqual(result['execution'], 'sequential')

    def test_cached_schema_pushes_native_filters_and_retains_local_filtering(self):
        cfg = self.cli_config()
        client = self.client()
        schema = {'paths': {'/api/v3/episode': {'get': {'parameters': [
            {'name': 'seriesId', 'in': 'query', 'schema': {'type': 'integer'}},
            {'name': 'episodeIds', 'in': 'query', 'schema': {'type': 'array'}}]}}}}
        private_json(Catalog(client).path, schema)
        # CLI uses the same config parent as this client's private state dir.
        self.route('GET', '/api/v3/episode', [{'id': 7, 'seriesId': 24}, {'id': 8, 'seriesId': 24}])
        code, result, err = self.invoke(['--config', str(cfg), 'sonarr', 'list', 'episode',
                                        '--filter', 'seriesId=24', '--filter', 'id in [7]'])
        self.assertEqual(code, 0, err)
        self.assertEqual(Handler.calls[-1]['query'], {'seriesId': ['24'], 'episodeIds': ['7']})
        self.assertEqual(result['matched'], 1)

    def test_regex_filter_is_not_pushed_as_native_equality(self):
        client = self.client()
        private_json(Catalog(client).path, {'paths': {'/api/v3/series': {'get': {'parameters': [
            {'name': 'title', 'in': 'query', 'schema': {'type': 'string'}}]}}}})
        self.assertEqual(server_filters(client, '/series', [], ['title=~"AHS|Horror"']), [])

    def missing_fixture(self):
        self.route('GET', '/api/v3/series/24', {'id': 24, 'title': 'AHS'})
        self.route('GET', '/api/v3/episode', [
            {'id': 6, 'seriesId': 24, 'seasonNumber': 13, 'episodeNumber': 6, 'title': 'E6', 'hasFile': False, 'monitored': True, 'airDateUtc': '2020-01-01T00:00:00Z'},
            {'id': 7, 'seriesId': 24, 'seasonNumber': 13, 'episodeNumber': 7, 'title': 'E7', 'hasFile': False, 'monitored': True, 'airDateUtc': '2020-01-01T00:00:00Z'},
            {'id': 8, 'seriesId': 24, 'seasonNumber': 13, 'episodeNumber': 8, 'title': 'Future', 'hasFile': False, 'monitored': True, 'airDateUtc': '2099-01-01T00:00:00Z'},
            {'id': 9, 'seriesId': 24, 'seasonNumber': 0, 'episodeNumber': 1, 'title': 'Special', 'hasFile': False, 'monitored': False, 'airDateUtc': '2020-01-01T00:00:00Z'}])
        releases = [{'title': 'AHS.S13E06.VOSTFR', 'guid': 'a', 'indexer': 'test', 'indexerId': 1,
                     'mappedSeriesId': 24, 'seasonNumber': 13, 'episodeNumbers': [6], 'seeders': 5},
                    {'title': 'AHS.S13E07.English', 'guid': 'b', 'indexer': 'test', 'indexerId': 1,
                     'mappedSeriesId': 24, 'seasonNumber': 13, 'episodeNumbers': [7], 'seeders': 10},
                    {'title': 'Wrong.Show.S13E07.VOSTFR', 'guid': 'c', 'indexer': 'test', 'indexerId': 1,
                     'mappedSeriesId': 999, 'seasonNumber': 13, 'episodeNumbers': [7], 'seeders': 20}]
        self.route('GET', '/api/v3/release', releases)
        return releases

    def test_find_missing_searches_once_per_season_and_never_grabs(self):
        cfg = self.cli_config()
        self.missing_fixture()
        code, result, err = self.invoke(['--config', str(cfg), 'sonarr', 'find-missing', '--series-id', '24', '--vostfr'])
        self.assertEqual(code, 0, err)
        self.assertEqual((result['missing'], result['searches'], result['downloadsStarted']), (2, 1, 0))
        self.assertEqual([e['vostfrLabelled'] for e in result['episodes']], [1, 0])
        self.assertFalse(result['episodes'][0]['candidates'][0]['frenchSubtitlesVerified'])
        self.assertEqual(sum(c['path'] == '/api/v3/release' for c in Handler.calls), 1)
        self.assertTrue(all(c['method'] == 'GET' for c in Handler.calls))

    def test_find_missing_reuses_export_without_search_and_no_cache_is_respected(self):
        cfg = self.cli_config()
        releases = self.missing_fixture()
        file = self.directory / 'releases.json'
        file.write_text(json.dumps(releases))
        code, result, err = self.invoke(['--config', str(cfg), '--no-cache', 'sonarr', 'find-missing', '--series-id', '24',
                                        '--release-file', str(file), '--vostfr'])
        self.assertEqual(code, 0, err)
        self.assertEqual(result['searches'], 0)
        self.assertFalse(any(c['path'] == '/api/v3/release' for c in Handler.calls))
        self.assertFalse((cfg.parent / 'results').exists())


if __name__ == '__main__':
    unittest.main()
