import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SECRET = re.compile(r'(?i)(?:password|passwd|passkey|auth.?key|api.?key|access.?token|refresh.?token|session.?secret|client.?secret|authorization|cookie|private.?key|secret|^token$|^tokenhash$|^salt$|^t$|^p$)')


class Redactor:
    def __init__(self, config=None):
        self.secrets = set()
        self.collect(config or {})

    def collect(self, value, key=''):
        if isinstance(value, dict):
            sensitive_field = SECRET.search(str(value.get('name', value.get('key', '')))) or value.get('privacy') in ('password', 'apiKey')
            for k, v in value.items():
                self.collect(v, 'secret' if k == 'value' and sensitive_field else k)
        elif isinstance(value, list):
            for v in value:
                self.collect(v, key)
        elif isinstance(value, str) and not key.endswith('_env') and SECRET.search(key) and len(value) >= 4:
            self.secrets.add(value)

    def clean(self, value, key=''):
        if SECRET.search(key) and not key.endswith('_env') and not isinstance(value, (dict, list)) and value is not None and value != '':
            return '[REDACTED]'
        if isinstance(value, dict):
            sensitive_field = SECRET.search(str(value.get('name', value.get('key', '')))) or value.get('privacy') in ('password', 'apiKey')
            return {k: self.clean(v, 'secret' if k == 'value' and sensitive_field else k) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.clean(v) for v in value]
        if isinstance(value, str):
            for secret in sorted(self.secrets, key=len, reverse=True):
                value = value.replace(secret, '[REDACTED]')
            value = re.sub(r'(?i)([?&](?:apikey|api_key|api-key|passkey|authkey|token|password|access_token|t|p|s)=)[^&\s\"<>]+', r'\1[REDACTED]', value)
            value = re.sub(r'(https?://)[^/@\s]+:[^/@\s]+@', r'\1[REDACTED]@', value)
        return value
