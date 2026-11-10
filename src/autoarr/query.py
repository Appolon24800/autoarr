"""Conservative filter pushdown using only parameters advertised by cached APIs."""
import json
import re

from .catalog import Catalog
from .output import parse_value


def server_filters(client, path, query, filters):
    if not filters:
        return query
    catalog = Catalog(client)
    if not catalog.path.exists():
        return query
    try:
        catalog.spec = json.loads(catalog.path.read_text())
        prefix = client.app['api_prefix']
        op = catalog.spec['paths'].get(prefix + path, catalog.spec['paths'].get(path, {}))
        parameters = catalog.resolve(op.get('parameters', []) + op.get('get', {}).get('parameters', []))
        allowed = {p['name']: p for p in parameters if p.get('in') == 'query'}
    except (OSError, ValueError, KeyError, TypeError):
        return query
    result = list(query)
    present = {k.casefold() for k, v in query}
    for expression in filters:
        match = re.fullmatch(r'\s*([A-Za-z][A-Za-z0-9]*)\s*=(?![~=])\s*(.*?)\s*', expression)
        if match:
            name, text = match.groups()
            value = parse_value(text)
            if name in allowed and name.casefold() not in present and not isinstance(value, (dict, list)):
                result.append((name, value))
                present.add(name.casefold())
            continue
        # Only advertise the ID-array alias for the exact corresponding resource.
        match = re.fullmatch(r'\s*id\s+in\s+(\[.*\])\s*', expression)
        aliases = {'/episode': 'episodeIds', '/episodefile': 'episodeFileIds',
                   '/moviefile': 'movieFileIds', '/trackfile': 'trackFileIds'}
        if match and aliases.get(path) in allowed:
            name = aliases[path]
            value = parse_value(match[1])
            if name.casefold() not in present and isinstance(value, list):
                result.extend((name, v) for v in value)
                present.add(name.casefold())
    return result
