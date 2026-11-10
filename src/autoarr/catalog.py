import hashlib
import json
import re
import time
import urllib.error
import urllib.request

from .client import ApiError
from .config import AutoarrError, HTTP_METHODS, private_json

SCHEMA_PATHS = {
    'jellyfin': ['/api-docs/openapi.json', '/api-docs/swagger.json'],
    'bazarr': ['/api/swagger.json'],
    'seerr': ['/api-docs/swagger-ui-init.js', '/api-docs/swagger.json'],
    'cleanuparr': ['/swagger/v1/swagger.json', '/openapi/v1.json'],
    'autobrr': ['/api/swagger.json', '/swagger/doc.json'],
}
PUBLISHED_SCHEMAS = {
    'radarr': ('Radarr/Radarr', 'src/Radarr.Api.V3/openapi.json'),
    'sonarr': ('Sonarr/Sonarr', 'src/Sonarr.Api.V3/openapi.json'),
    'lidarr': ('Lidarr/Lidarr', 'src/Lidarr.Api.V1/openapi.json'),
    'prowlarr': ('Prowlarr/Prowlarr', 'src/Prowlarr.Api.V1/openapi.json'),
    'bookshelf': ('pennydreadful/bookshelf', 'src/Readarr.Api.V1/openapi.json'),
    'readarr': ('Readarr/Readarr', 'src/Readarr.Api.V1/openapi.json'),
}


class Catalog:
    def __init__(self, client):
        self.client = client
        self.spec = None
        self._operations = None
        self._operations_by_id = None
        fingerprint = hashlib.sha256(client.app['url'].encode()).hexdigest()[:12]
        self.path = client.state_dir / 'schemas' / (client.app['name'] + '-' + fingerprint + '.json')

    def load(self, refresh=False):
        if refresh:
            self._operations = None
            self._operations_by_id = None
        if self.spec is not None and not refresh:
            return self.spec
        if not refresh and self.path.exists() and time.time() - self.path.stat().st_mtime < 86400:
            self.spec = json.loads(self.path.read_text())
            return self.spec
        candidates = [self.client.app['schema_path']] if self.client.app.get('schema_path') else SCHEMA_PATHS.get(
            self.client.kind, [self.client.app['api_prefix'] + '/openapi.json', '/swagger/v1/swagger.json', '/swagger.json', '/openapi.json'])
        errors = []
        for path in candidates:
            try:
                data = self.client.request('GET', path, site=True)
                if isinstance(data, str) and 'swagger-ui-init.js' in path:
                    match = re.search(r'var\s+options\s*=\s*', data)
                    if match:
                        options, _ = json.JSONDecoder().raw_decode(data[match.end():])
                        data = options.get('swaggerDoc', {})
                if isinstance(data, dict) and isinstance(data.get('paths'), dict):
                    data['x-autoarr-source'] = {'type': 'live', 'url': self.client.app['url'] + path}
                    self.spec = data
                    private_json(self.path, data)
                    return data
                errors.append(path + ': not OpenAPI JSON')
            except ApiError as e:
                errors.append(path + ': HTTP ' + str(e.status))
        # Arr publishes OpenAPI in its official source repository, rather than
        # serving it in many production builds. Never send application auth to
        # this public documentation host. Prefer an exact version tag, and make
        # a develop-branch fallback explicit in schema provenance.
        if self.client.kind in PUBLISHED_SCHEMAS:
            repository, file = PUBLISHED_SCHEMAS[self.client.kind]
            status = self.client.request('GET', '/system/status')
            version = status.get('version', '')
            refs = ['v' + version, 'develop'] if version else ['develop']
            for ref in refs:
                source = f'https://raw.githubusercontent.com/{repository}/{ref}/{file}'
                try:
                    with urllib.request.urlopen(source, timeout=20) as response:
                        data = json.loads(response.read(16 * 1024 * 1024))
                    if not isinstance(data.get('paths'), dict):
                        continue
                    data['x-autoarr-source'] = {'type': 'official-published', 'url': source,
                        'serverVersion': version, 'exactVersionTag': ref == 'v' + version}
                    self.spec = data
                    private_json(self.path, data)
                    return data
                except (urllib.error.URLError, TimeoutError, ValueError) as e:
                    if isinstance(e, urllib.error.HTTPError):
                        e.close()
                    errors.append(source + ': unavailable')
        if not refresh and self.path.exists():
            self.spec = json.loads(self.path.read_text())
            return self.spec
        raise AutoarrError('No OpenAPI schema found. Use api for arbitrary endpoints or configure schema_path. Tried: ' + '; '.join(errors))

    def resolve(self, value, seen=None, depth=0):
        seen = set() if seen is None else seen
        if depth > 12:
            return value
        if isinstance(value, list):
            return [self.resolve(v, seen, depth + 1) for v in value]
        if not isinstance(value, dict):
            return value
        ref = value.get('$ref')
        if ref and ref.startswith('#/') and ref not in seen:
            target = self.load()
            for part in ref[2:].split('/'):
                target = target[part.replace('~1', '/').replace('~0', '~')]
            return self.resolve({**target, **{k: v for k, v in value.items() if k != '$ref'}}, seen | {ref}, depth + 1)
        return {k: self.resolve(v, seen, depth + 1) for k, v in value.items()}

    def operations(self, search='', method=None, refresh=False, details=False):
        spec = self.load(refresh)
        if self._operations is None:
            self._operations = []
            for path, methods in spec['paths'].items():
                for verb, op in methods.items():
                    if verb.upper() not in HTTP_METHODS or not isinstance(op, dict):
                        continue
                    relative = path.removeprefix(self.client.app['api_prefix'])
                    generated = verb + '_' + re.sub(r'[^A-Za-z0-9]+', '_', relative).strip('_')
                    self._operations.append({'operationId': op.get('operationId', generated),
                        'method': verb.upper(), 'path': path,
                        'summary': op.get('summary', ''), 'tags': op.get('tags', [])})
            self._operations.sort(key=lambda x: (x['path'], x['method']))
            self._operations_by_id = {}
            for row in self._operations:
                self._operations_by_id.setdefault(row['operationId'].casefold(), []).append(row)
        result = []
        for row in self._operations:
            if method and method.upper() != row['method']:
                continue
            if search.casefold() not in ' '.join(str(v) for v in row.values()).casefold():
                continue
            if details:
                from .output import schema_brief
                result.append({**row, **schema_brief(self.operation(row['operationId'], include_responses=False))})
            else:
                result.append(dict(row))
        return result

    def operation(self, operation_id, include_responses=True):
        if self._operations is None:
            self.operations()
        matches = self._operations_by_id.get(operation_id.casefold(), [])
        if not matches:
            raise AutoarrError('Unknown operation ID; use ops --search TERM')
        if len(matches) != 1:
            raise AutoarrError('Ambiguous operation ID; use api METHOD PATH')
        row = dict(matches[0])
        path_item = self.spec['paths'][row['path']]
        raw = path_item[row['method'].lower()]
        row['parameters'] = self.resolve(path_item.get('parameters', []) + raw.get('parameters', []))
        row['requestBody'] = self.resolve(raw.get('requestBody', {}))
        if include_responses:
            row['responses'] = self.resolve(raw.get('responses', {}))
        row['schemaSource'] = self.spec.get('x-autoarr-source')
        return row

    def invoke(self, operation_id, paths, query, body=None, form=None, raw=None, files=None, content_type=None, output=None):
        from urllib.parse import quote
        op = self.operation(operation_id, include_responses=False)
        path = op['path']
        required_paths = set(re.findall(r'\{([^}]+)\}', path))
        if set(paths) != required_paths:
            raise AutoarrError('Path parameters must be exactly: ' + ', '.join(sorted(required_paths)))
        for k, v in paths.items():
            path = path.replace('{' + k + '}', quote(str(v), safe=''))
        names = {k for k, v in query}
        form_names = {k for k, v in (form or []) + (files or [])}
        for p in op['parameters']:
            if p.get('in') == 'query' and p.get('required') and p['name'] not in names:
                raise AutoarrError('Missing required query parameter: ' + p['name'])
            if p.get('in') == 'formData' and p.get('required') and p['name'] not in form_names:
                raise AutoarrError('Missing required form field: ' + p['name'])
        serialized_query = []
        query_specs = {p['name']: p for p in op['parameters'] if p.get('in') == 'query'}
        for key, value in query:
            parameter = query_specs.get(key, {})
            schema = parameter.get('schema', parameter)
            if isinstance(value, list) and schema.get('type') == 'array':
                explode = parameter.get('collectionFormat') == 'multi' if 'swagger' in self.spec else parameter.get('explode', True)
                if explode:
                    serialized_query.extend((key, item) for item in value)
                else:
                    from .client import scalar
                    separator = {'ssv': ' ', 'tsv': '\t', 'pipes': '|'}.get(parameter.get('collectionFormat'), ',')
                    serialized_query.append((key, separator.join(scalar(v) for v in value)))
            else:
                serialized_query.append((key, value))
        if op['requestBody'].get('required') and all(v is None for v in (body, form, raw, files)):
            raise AutoarrError('This operation requires --data, --form, or --upload; inspect schema OPERATION_ID')
        prefix = self.client.app['api_prefix']
        absolute_paths = bool(prefix) and any(p == prefix or p.startswith(prefix + '/') for p in self.spec['paths'])
        return self.client.request(op['method'], path, query=serialized_query, body=body,
            form=form, raw=raw, files=files, content_type=content_type, output=output, site=absolute_paths)
