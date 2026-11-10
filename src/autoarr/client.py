import base64
import hashlib
import http.cookiejar
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__
from .config import AutoarrError, HTTP_METHODS


class ApiError(AutoarrError):
    def __init__(self, status, method, path, detail=None):
        self.status = status
        self.detail = detail
        super().__init__(f'HTTP {status}: {method} {path}' + (': ' + json.dumps(detail, ensure_ascii=False) if detail else ''))


class NoForeignRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(newurl)
        if (old.scheme, old.hostname, old.port) != (new.scheme, new.hostname, new.port):
            fp.close()
            raise AutoarrError('Refusing a cross-origin API redirect; check the configured base URL')
        if req.get_method() not in ('GET', 'HEAD'):
            fp.close()
            raise AutoarrError('Refusing to redirect a write request; check the configured base URL')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def scalar(value):
    if isinstance(value, bool):
        return str(value).lower()
    if value is None:
        return ''
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(',', ':'))
    return str(value)


class Client:
    def __init__(self, app, state_dir, redactor, timeout=90, dry_run=False):
        self.app = app
        self.kind = app['kind']
        self.state_dir = Path(state_dir)
        self.redactor = redactor
        self.redactor.collect(app)
        self.timeout = timeout
        self.dry_run = dry_run
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(NoForeignRedirects(), urllib.request.HTTPCookieProcessor(self.cookies))
        self.authenticated = False

    def url(self, path, site=False):
        parts = urllib.parse.urlsplit(path)
        if parts.scheme or parts.netloc or parts.fragment or path.startswith('//') or '\\' in path:
            raise AutoarrError('API paths must be relative to the configured application')
        decoded = parts.path
        for _ in range(3):
            decoded = urllib.parse.unquote(decoded)
        if any(x in ('.', '..') for x in decoded.split('/')) or decoded.startswith('//'):
            raise AutoarrError('Path traversal is not allowed in API paths')
        path = '/' + parts.path.lstrip('/')
        prefix = self.app['api_prefix'].rstrip('/')
        if not site and prefix and path != prefix and not path.startswith(prefix + '/'):
            path = prefix + path
        return self.app['url'] + path, urllib.parse.parse_qsl(parts.query, keep_blank_values=True)

    def journal(self, event):
        self.redactor.collect(event)
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.state_dir / 'requests.jsonl'
        flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, 'a') as f:
            f.write(json.dumps(self.redactor.clean({'time': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'app': self.app['name'], **event}), ensure_ascii=False) + '\n')

    def request(self, method, path, query=None, body=None, form=None, raw=None, files=None,
                content_type=None, site=False, output=None, read_only=False):
        method = method.upper()
        if method not in HTTP_METHODS:
            raise AutoarrError('Unsupported HTTP method: ' + method)
        if output:
            dest = Path(output)
            if dest.exists() or dest.is_symlink():
                raise AutoarrError('Output file already exists: ' + str(dest))
            if not dest.parent.is_dir() or not os.access(dest.parent, os.W_OK):
                raise AutoarrError('Output directory is not writable: ' + str(dest.parent))
        url, embedded = self.url(path, site)
        pairs = embedded + list(query or [])
        is_write = method not in ('GET', 'HEAD', 'OPTIONS') and not read_only
        if self.kind == 'navidrome':
            endpoint = urllib.parse.urlsplit(path).path.rsplit('/', 1)[-1].removesuffix('.view')
            is_write = is_write or endpoint.startswith(('create', 'update', 'delete', 'set', 'change', 'start', 'star', 'unstar', 'scrobble', 'jukeboxControl'))
        if sum(v is not None for v in (body, form, raw)) > 1:
            raise AutoarrError('Use only one of JSON data, form data, or a binary upload')
        if files and (body is not None or raw is not None):
            raise AutoarrError('Multipart files can be combined with --form, but not JSON or a raw upload')
        plan = {'method': method, 'url': url, 'query': pairs}
        if body is not None:
            plan['body'] = body
        if form is not None:
            plan['form'] = form
        if raw is not None:
            plan['uploadBytes'] = len(raw)
        if files:
            plan['files'] = [{'field': field, 'path': str(filename), 'bytes': Path(filename).stat().st_size} for field, filename in files]
        if self.dry_run and is_write:
            return {'dryRun': True, 'request': self.redactor.clean(plan)}

        headers = {'Accept': 'application/json', 'User-Agent': 'autoarr-cli/' + __version__}
        key = self.app.get('api_key', '')
        auth = self.app.get('auth')
        if auth == 'jellyfin':
            headers['Authorization'] = 'MediaBrowser Token="' + key + '"'
        elif auth == 'bearer':
            headers['Authorization'] = 'Bearer ' + key
        elif auth == 'query':
            pairs.append(('api_key', key))
        elif auth == 'subsonic':
            username = self.app.get('username')
            password = self.app.get('password')
            if not username or not (password or key):
                raise AutoarrError('Navidrome needs a username and password/password_env, or an OpenSubsonic API key; configure them privately')
            pairs += [('u', username), ('v', '1.16.1'), ('c', 'autoarr'), ('f', 'json')]
            if key:
                pairs.append(('apiKey', key))
            else:
                salt = secrets.token_hex(12)
                token = hashlib.md5((password + salt).encode()).hexdigest()
                pairs += [('s', salt), ('t', token)]
        elif auth != 'none':
            headers[self.app.get('api_key_header', 'X-Api-Key')] = key
        if self.kind == 'qbittorrent':
            headers['Referer'] = self.app['url'] + '/'
            headers['Origin'] = self.app['url']
            if self.app.get('username') and not self.authenticated:
                password = self.app.get('password', '')
                login = urllib.request.Request(self.app['url'] + self.app['api_prefix'] + '/auth/login',
                    data=urllib.parse.urlencode({'username': self.app['username'], 'password': password}).encode(),
                    headers=headers, method='POST')
                try:
                    with self.opener.open(login, timeout=self.timeout) as response:
                        if response.read().decode().strip() != 'Ok.':
                            raise AutoarrError('qBittorrent login failed')
                except urllib.error.URLError:
                    raise AutoarrError('qBittorrent login failed') from None
                self.authenticated = True
        data = None
        if files:
            import mimetypes
            boundary = 'autoarr-' + secrets.token_hex(20)
            chunks = []
            def quoted(value):
                value = str(value)
                if '\r' in value or '\n' in value:
                    raise AutoarrError('Invalid multipart field or filename')
                return value.replace('\\', '\\\\').replace('"', '\\"')
            for field, value in form or []:
                chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{quoted(field)}"\r\n\r\n{scalar(value)}\r\n').encode())
            for field, filename in files:
                file = Path(filename)
                mime = mimetypes.guess_type(file.name)[0] or 'application/octet-stream'
                chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{quoted(field)}"; filename="{quoted(file.name)}"\r\nContent-Type: {mime}\r\n\r\n').encode())
                chunks += [file.read_bytes(), b'\r\n']
            chunks.append(f'--{boundary}--\r\n'.encode())
            data = b''.join(chunks)
            headers['Content-Type'] = 'multipart/form-data; boundary=' + boundary
        elif body is not None:
            if self.kind == 'qbittorrent':
                raise AutoarrError('qBittorrent expects --form fields, not JSON --data')
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        elif form is not None:
            data = urllib.parse.urlencode([(k, scalar(v)) for k, v in form], doseq=True).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        elif raw is not None:
            data = raw
            headers['Content-Type'] = content_type or 'application/octet-stream'
        if pairs:
            url += '?' + urllib.parse.urlencode([(k, scalar(v)) for k, v in pairs])
        request = urllib.request.Request(url, headers=headers, data=data, method=method)
        if is_write:
            self.journal({'phase': 'started', 'request': plan})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if not site and not output and 'text/html' in response.headers.get('Content-Type', '').lower():
                    raise AutoarrError('API returned HTML instead of data; check authentication and the configured URL base')
                if output:
                    dest = Path(output)
                    if dest.exists():
                        raise AutoarrError('Output file already exists: ' + str(dest))
                    size = 0
                    with dest.open('xb') as f:
                        os.chmod(dest, 0o600)
                        while chunk := response.read(1024 * 1024):
                            f.write(chunk)
                            size += len(chunk)
                    result = {'savedTo': str(dest), 'bytes': size, 'httpStatus': response.status}
                else:
                    data = response.read()
                    if not data:
                        result = {'httpStatus': response.status}
                    else:
                        try:
                            result = json.loads(data)
                        except (ValueError, UnicodeDecodeError):
                            try:
                                result = data.decode('utf-8')
                            except UnicodeDecodeError:
                                raise AutoarrError('Binary response; use --output FILE') from None
                if isinstance(result, dict) and 'subsonic-response' in result:
                    envelope = result['subsonic-response']
                    if envelope.get('status') != 'ok':
                        raise AutoarrError('Subsonic API error: ' + json.dumps(envelope.get('error')))
                if is_write:
                    self.journal({'phase': 'completed', 'method': method, 'path': path, 'httpStatus': response.status})
                return result
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read(16384))
            except (ValueError, UnicodeDecodeError):
                detail = {'message': e.reason}
            finally:
                e.close()
            if is_write:
                self.journal({'phase': 'failed', 'method': method, 'path': path, 'httpStatus': e.code})
            raise ApiError(e.code, method, path, self.redactor.clean(detail)) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if is_write:
                self.journal({'phase': 'uncertain', 'method': method, 'path': path})
            raise AutoarrError('Request failed for ' + self.app['name'] + ': ' + str(e) +
                               ('; write outcome may be uncertain, inspect before retrying' if is_write else '')) from None
