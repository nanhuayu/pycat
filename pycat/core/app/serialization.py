"""JSON values for application projections, with explicit secret preservation."""
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path

SECRET = '__secret__'
_SECRET_KEYS = {'api_key', 'password', 'token', 'access_token', 'refresh_token', 'client_secret',
                'secret', 'bot_token', 'app_secret', 'authorization', 'encrypt_key', 'verification_token'}


def json_value(value):
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, 'to_dict'):
        return json_value(value.to_dict())
    if is_dataclass(value):
        return {field.name: json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f'Unsupported JSON value: {type(value).__name__}')


def redact(value, *, secret=False):
    if isinstance(value, dict):
        return {key: redact(item, secret=secret or key.lower() in _SECRET_KEYS or key.lower().endswith(('_token', '_secret', '_password', '_aes_key')) or key.lower() in {'headers', 'custom_headers', 'env'})
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, secret=secret) for item in value]
    return SECRET if secret and value else value


def merge_draft(current, patch, *, replace_map=False):
    """Lists replace; redacted values retain the matching stable identity only."""
    if patch == SECRET:
        if current is None:
            raise ValueError('A new secret requires a value.')
        return current
    if isinstance(patch, dict):
        previous = current if isinstance(current, dict) else {}
        return {**({} if replace_map else previous), **{key: merge_draft(previous.get(key), item,
                replace_map=key in {'env', 'headers', 'custom_headers'}) for key, item in patch.items()}}
    if isinstance(patch, list):
        previous = current if isinstance(current, list) else []
        indexed = {item.get('id', item.get('slug', item.get('name'))): item for item in previous if isinstance(item, dict)}
        return [merge_draft(indexed.get(item.get('id', item.get('slug', item.get('name')))), item)
                if isinstance(item, dict) else item for item in patch]
    return patch
