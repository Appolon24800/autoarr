import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


class AutoarrError(Exception):
    pass


KINDS = {
    'radarr': {'prefix': '/api/v3', 'status': '/system/status', 'entity': 'movie'},
    'sonarr': {'prefix': '/api/v3', 'status': '/system/status', 'entity': 'series'},
    'lidarr': {'prefix': '/api/v1', 'status': '/system/status', 'entity': 'artist'},
    'bookshelf': {'prefix': '/api/v1', 'status': '/system/status', 'entity': 'author'},
    'readarr': {'prefix': '/api/v1', 'status': '/system/status', 'entity': 'author'},
    'prowlarr': {'prefix': '/api/v1', 'status': '/system/status'},
    'bazarr': {'prefix': '/api', 'status': '/system/status'},
    'jellyfin': {'prefix': '', 'status': '/System/Info'},
    'seerr': {'prefix': '/api/v1', 'status': '/status'},
    'qbittorrent': {'prefix': '/api/v2', 'status': '/app/version', 'auth': 'none'},
    'kapowarr': {'prefix': '/api', 'status': '/system/about', 'auth': 'query'},
    'cleanuparr': {'prefix': '/api', 'status': '/health'},
    'autobrr': {'prefix': '/api', 'status': '/config'},
    'navidrome': {'prefix': '/rest', 'status': '/ping.view', 'auth': 'subsonic'},
    'generic': {'prefix': '', 'status': '/', 'auth': 'header'},
}
ARR = {'radarr', 'sonarr', 'lidarr', 'bookshelf', 'readarr', 'prowlarr'}
MANAGERS = ARR - {'prowlarr'}
HTTP_METHODS = {'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'}


def config_path(explicit=None):
    if explicit or os.environ.get('AUTOARR_CONFIG'):
        return Path(explicit or os.environ['AUTOARR_CONFIG']).expanduser().resolve()
    local = Path.cwd() / '.local/autoarr/config.json'
    if local.exists():
        return local
    checkout = Path(__file__).resolve().parents[2] / '.local/autoarr/config.json'
    if checkout.exists():
        return checkout
    return Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'autoarr/config.json'


def private_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise AutoarrError('Refusing to overwrite a symlink: ' + str(path))
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_config(path, required=True):
    if not path.exists():
        if required:
            raise AutoarrError('No configuration. Run autoarr init or autoarr config add; see --help.')
        return {'version': 1, 'apps': {}}
    if path.stat().st_mode & 0o077:
        raise AutoarrError('Configuration must be private; run chmod 600 ' + str(path))
    try:
        result = json.loads(path.read_text())
    except (ValueError, OSError) as e:
        raise AutoarrError('Cannot read configuration: ' + str(e)) from None
    if result.get('version') != 1 or not isinstance(result.get('apps'), dict):
        raise AutoarrError('Expected configuration version 1 and an apps object')
    return result


def app_config(config, name):
    try:
        app = dict(config['apps'][name])
    except KeyError:
        raise AutoarrError(f'App {name!r} is not configured; run autoarr apps') from None
    kind = app.setdefault('kind', name)
    if kind not in KINDS:
        raise AutoarrError('Unknown app kind: ' + kind)
    env = 'AUTOARR_' + re.sub('[^A-Za-z0-9]', '_', name).upper()
    app['url'] = os.environ.get(env + '_URL', app.get('url', '')).rstrip('/')
    parsed = urlsplit(app['url'])
    try:
        parsed.port
    except ValueError:
        raise AutoarrError('App URL has an invalid port or missing slash before its base path') from None
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AutoarrError('App URL must be an HTTP(S) base URL without credentials, query, or fragment')
    app['api_key'] = os.environ.get(env + '_API_KEY', os.environ.get(app.get('api_key_env', ''), app.get('api_key', '')))
    if app.get('password_env'):
        app['password'] = os.environ.get(app['password_env'], '')
    app.setdefault('api_prefix', KINDS[kind]['prefix'])
    app.setdefault('api_key_header', 'X-API-Token' if kind == 'autobrr' else 'X-Api-Key')
    app.setdefault('auth', KINDS[kind].get('auth', 'header'))
    if kind == 'jellyfin':
        app['auth'] = 'jellyfin'
    app['name'] = name
    return app
