"""Bounded iLink media transport; session ownership stays with SessionContentService."""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
import time
from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image, UnidentifiedImageError

from pycat.core.channel.platforms.wechat.client import WeChatChannelClient, ilink_headers
from pycat.core.content.session_content import MAX_INPUT_FILE_BYTES
from pycat.models.filenames import safe_filename

CDN_BASE = 'https://novac2c.cdn.weixin.qq.com/c2c'
MAX_FILE_BYTES = MAX_INPUT_FILE_BYTES


def _cdn_url(value: str) -> str:
    if len(value) > 8192:
        raise ValueError('微信附件 URL 过长。')
    url = urlsplit(value)
    if (url.scheme != 'https' or url.hostname != 'novac2c.cdn.weixin.qq.com'
            or url.port not in (None, 443) or url.username or url.password or url.fragment):
        raise ValueError('微信附件 URL 不是受支持的 HTTPS CDN 地址。')
    return value


def _key(value: str) -> bytes:
    if len(value) > 128:
        raise ValueError('微信附件密钥格式无效。')
    if re.fullmatch(r'[0-9a-fA-F]{32}', value):
        return bytes.fromhex(value)
    raw = base64.b64decode(value, validate=True)
    if len(raw) == 32:
        raw = bytes.fromhex(raw.decode('ascii'))
    if len(raw) != 16:
        raise ValueError('微信附件密钥格式无效。')
    return raw


def _crypt(raw: bytes, key: bytes, *, encrypt: bool) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    if encrypt:
        padder = padding.PKCS7(128).padder()
        raw = padder.update(raw) + padder.finalize()
        operation = cipher.encryptor()
        return operation.update(raw) + operation.finalize()
    operation = cipher.decryptor()
    padded = operation.update(raw) + operation.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


class WeChatMediaClient:
    def __init__(self, *, transport=None):
        self._transport = transport

    def download(self, item: dict, destination: Path) -> dict:
        kind = int(item.get('type') or 0)
        if kind not in (2, 4):
            raise ValueError('微信附件类型暂不支持。')
        body = item.get('image_item' if kind == 2 else 'file_item') or {}
        if not isinstance(body, dict) or not isinstance(body.get('media', {}), dict):
            raise ValueError('微信附件描述格式无效。')
        media = body.get('media', {})
        try:
            declared_size = int(body.get('len') or 0)
        except (TypeError, ValueError):
            raise ValueError('微信附件大小格式无效。') from None
        if declared_size < 0 or declared_size > MAX_FILE_BYTES:
            raise ValueError('微信附件超过大小上限（25 MiB）。')
        name = safe_filename(body.get('file_name') or ('image.png' if kind == 2 else ''), 'attachment.bin', limit=120)
        parameter = str(media.get('encrypt_query_param') or '')
        url = str(media.get('full_url') or body.get('url') or '')
        if not url:
            if not parameter:
                raise ValueError('微信附件缺少下载参数。')
            url = CDN_BASE + '/download?' + urlencode({'encrypted_query_param': parameter})
        url = _cdn_url(url)
        key = _key(str(body.get('aeskey') or media.get('aes_key') or ''))
        raw = bytearray()
        deadline = time.monotonic() + 30
        try:
            with httpx.Client(timeout=30, follow_redirects=False, transport=self._transport) as client:
                with client.stream('GET', url) as response:
                    response.raise_for_status()
                    for chunk in response.iter_bytes():
                        if time.monotonic() > deadline:
                            raise ValueError('微信附件下载超时，请重试。')
                        raw.extend(chunk)
                        if len(raw) > MAX_FILE_BYTES + 16:
                            raise ValueError('微信附件超过大小上限（25 MiB）。')
        except httpx.HTTPError:
            raise RuntimeError('微信附件下载失败，请重新发送。') from None
        try:
            plain = _crypt(bytes(raw), key, encrypt=False)
        except ValueError:
            raise ValueError('微信附件解密失败，文件未保存。') from None
        if len(plain) > MAX_FILE_BYTES:
            raise ValueError('微信附件超过大小上限（25 MiB）。')
        if body.get('len') and declared_size != len(plain):
            raise ValueError('微信附件大小校验失败，文件未保存。')
        if body.get('md5') and hashlib.md5(plain).hexdigest() != str(body['md5']).lower():
            raise ValueError('微信附件校验失败，文件未保存。')
        mime = ''
        if kind == 2:
            try:
                with Image.open(BytesIO(plain)) as picture:
                    extensions = {'JPEG': '.jpg', 'PNG': '.png', 'GIF': '.gif', 'WEBP': '.webp', 'BMP': '.bmp'}
                    if picture.format not in extensions or picture.width * picture.height > 40_000_000:
                        raise ValueError('微信图片格式或尺寸不受支持。')
                    name = 'image' + extensions[picture.format]
                    mime = Image.MIME[picture.format]
                    picture.verify()
            except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
                raise ValueError('微信图片无法解析，文件未保存。') from None
        # The display name never becomes a writable path supplied by the sender.
        suffix = Path(name).suffix[:16]
        path = destination / (secrets.token_hex(16) + suffix)
        path.write_bytes(plain)
        return {'path': str(path), 'name': name, **({'mime': mime} if mime else {})}

    def upload(self, channel, recipient: str, path: Path, *, expected_digest='', name='') -> dict:
        credentials = WeChatChannelClient.resolve_ilink_credentials(channel)
        if not credentials or not recipient:
            raise ValueError('微信文件发送需要已连接的账号和目标联系人。')
        with path.open('rb') as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError('微信附件超过大小上限（25 MiB）。')
        if expected_digest and hashlib.sha256(raw).hexdigest() != expected_digest:
            raise ValueError('交付文件已更改，请重新确认文件后发送。')
        key = secrets.token_bytes(16)
        filekey = secrets.token_hex(16)
        encrypted = _crypt(raw, key, encrypt=True)
        payload = {
            'filekey': filekey, 'media_type': 3, 'to_user_id': recipient,
            'rawsize': len(raw), 'rawfilemd5': hashlib.md5(raw).hexdigest(),
            'filesize': len(encrypted), 'no_need_thumb': True, 'aeskey': key.hex(),
            'base_info': WeChatChannelClient.base_info(),
        }
        try:
            with httpx.Client(timeout=30, follow_redirects=False, transport=self._transport) as client:
                response = client.post(credentials['api_base'] + '/ilink/bot/getuploadurl',
                                       json=payload, headers=ilink_headers(token=credentials['token']))
                response.raise_for_status()
                data = WeChatChannelClient.read_json(response, operation='upload file')
                parameter = str(data.get('upload_param') or '')
                full_url = str(data.get('upload_full_url') or '')
                if not (full_url or parameter):
                    raise ValueError('微信服务未返回文件上传地址。')
                url = _cdn_url(full_url or CDN_BASE + '/upload?' + urlencode({
                    'encrypted_query_param': parameter, 'filekey': filekey,
                }))
                response = client.post(url, content=encrypted, headers={'Content-Type': 'application/octet-stream'})
                response.raise_for_status()
                reference = response.headers.get('x-encrypted-param', '')
                if not reference:
                    raise ValueError('微信文件上传未返回有效引用。')
        except httpx.HTTPError:
            raise RuntimeError('微信文件上传失败，请稍后重试。') from None
        return {'type': 4, 'file_item': {
            'media': {'encrypt_query_param': reference, 'aes_key': base64.b64encode(key.hex().encode()).decode(),
                      'encrypt_type': 1},
            'file_name': safe_filename(name or path.name, 'attachment.bin', limit=120), 'len': str(len(raw)),
        }}
