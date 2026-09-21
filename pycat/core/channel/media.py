"""Shared bounded attachment snapshots and explicit current-turn delivery.

Platform clients own authentication and wire formats; this module never runs an
Agent, interprets message text as a path, or stores platform download credentials.
"""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
import hashlib
from io import BytesIO
import mimetypes
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from PIL import Image, UnidentifiedImageError

from pycat.core.channel.replies import channel_reply_policy
from pycat.core.content.references import delivery_refs_for_messages
from pycat.core.content.session_content import MAX_INPUT_BATCH_BYTES, MAX_INPUT_FILE_BYTES
from pycat.models.workspace import workspace_identity


MAX_MEDIA_ITEMS = 8


@dataclass(frozen=True)
class OutboundFile:
    name: str
    mime: str
    data: bytes


def safe_filename(value: str, default='attachment.bin') -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value or '').replace('\\', '/').rsplit('/', 1)[-1])
    name = name.strip(' .')[:180]
    return name or default


def read_media_response(response, *, limit=MAX_INPUT_FILE_BYTES) -> bytes:
    response.raise_for_status()
    if int(response.headers.get('Content-Length', '0')) > limit:
        raise ValueError('附件大小超过限制。')
    data = bytearray()
    for chunk in response.iter_bytes(64 * 1024):
        if len(data) + len(chunk) > limit:
            raise ValueError('附件大小超过限制。')
        data.extend(chunk)
    if not data:
        raise ValueError('附件为空。')
    return bytes(data)


def media_url(value, domains) -> str:
    value = str(value or '')
    url = urlsplit(value)
    host = (url.hostname or '').lower()
    if (len(value) > 16384 or url.scheme != 'https' or url.username or url.password or url.fragment
            or url.port not in (None, 443)
            or not any(host == domain or host.endswith('.' + domain) for domain in domains)):
        raise ValueError('附件不是受支持的 HTTPS 平台地址。')
    return value


def download_public_media(client, url, *, domains, limit=MAX_INPUT_FILE_BYTES):
    """Only follow redirects within the platform CDN; never forward API tokens."""
    for _ in range(4):
        url = media_url(url, domains)
        with client.stream('GET', url, follow_redirects=False) as response:
            if response.is_redirect:
                url = str(response.url.join(response.headers['location']))
                continue
            return read_media_response(response, limit=limit)
    raise ValueError('附件重定向次数过多。')


def save_attachment(raw, directory, *, name='', mime='', image=False):
    if not raw or len(raw) > MAX_INPUT_FILE_BYTES:
        raise ValueError('附件大小超过限制或为空。')
    name = safe_filename(name)
    if image:
        try:
            with Image.open(BytesIO(raw)) as picture:
                detected = picture.format
                picture.verify()
            mime = Image.MIME.get(detected, '')
            if not mime.startswith('image/'):
                raise ValueError('图片格式无效。')
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise ValueError('图片格式无效。') from exc
        suffix = '.jpg' if mime == 'image/jpeg' else mimetypes.guess_extension(mime) or '.img'
        if name == 'attachment.bin' or not Path(name).suffix:
            name = 'image' + suffix if name == 'attachment.bin' else name + suffix
    mime = mime or mimetypes.guess_type(name)[0] or 'application/octet-stream'
    path = Path(directory) / (uuid4().hex + Path(name).suffix)
    path.write_bytes(raw)
    return {'path': str(path), 'name': name, 'mime': mime}


class ChannelMediaTransfer:
    def __init__(self, content_resolver=None):
        self._resolver = content_resolver

    def process(self, context, channel, message, *, download, send_file, reply_sender,
                binding_updates=None, **binding):
        media = message.metadata.pop('_channel_media', [])
        replies = channel_reply_policy(channel) not in {'none', 'silent', 'off'}
        updates = {**(binding_updates or {}), 'file_delivery': replies and send_file is not None}
        with TemporaryDirectory(prefix='pycat-channel-') if media else nullcontext(None) as temporary:
            attachments = []
            try:
                if not isinstance(media, list) or len(media) > MAX_MEDIA_ITEMS:
                    raise ValueError('一次最多接收 8 个附件。')
                total = 0
                for item in media:
                    attachment = download(item, Path(temporary))
                    size = Path(attachment['path']).stat().st_size
                    total += size
                    if size > MAX_INPUT_FILE_BYTES or total > MAX_INPUT_BATCH_BYTES:
                        raise ValueError('附件大小超过限制。')
                    attachments.append(attachment)
            except (ValueError, OSError, RuntimeError, httpx.HTTPError):
                if replies:
                    reply_sender('附件接收失败，请检查机器人资源权限、文件类型和大小后重新发送。每个文件上限 25 MiB，每次最多 8 个，合计 64 MiB；平台可能有更小限制。')
                return None
            processed = context.process_bound_channel_message(channel, message, attachments=attachments,
                binding_updates=updates, reply_sender=reply_sender, **binding)
        if processed and replies and send_file is not None:
            self.send_outputs(processed[0], message, send_file, reply_sender)
        return processed

    def send_outputs(self, conversation, anchor, send_file, reply_sender):
        start = next((i for i, item in enumerate(conversation.messages) if item.id == anchor.id), None)
        if start is None:
            return
        # Do not include a later turn even if a caller supplies an older anchor.
        end = next((i for i in range(start + 1, len(conversation.messages))
                    if conversation.messages[i].role == 'user'), len(conversation.messages))
        refs = delivery_refs_for_messages(conversation.messages[start + 1:end])
        total = 0
        for index, ref in enumerate(refs):
            try:
                if index >= MAX_MEDIA_ITEMS or self._resolver is None:
                    raise ValueError('文件投递超过限制或不可用。')
                if ref.kind in {'archive', 'artifact'} and ref.conversation_id != conversation.id:
                    raise ValueError('交付文件不属于当前会话。')
                if ref.workspace and workspace_identity(ref.workspace) != workspace_identity(conversation.work_dir):
                    raise ValueError('交付文件不属于当前工作区。')
                path = self._resolver.resolve(conversation, ref)
                with path.open('rb') as stream:
                    raw = stream.read(MAX_INPUT_FILE_BYTES + 1)
                total += len(raw)
                if not raw or len(raw) > MAX_INPUT_FILE_BYTES or total > MAX_INPUT_BATCH_BYTES:
                    raise ValueError('文件大小超过限制。')
                if len(ref.digest) != 64 or hashlib.sha256(raw).hexdigest() != ref.digest:
                    raise ValueError('文件已改变。')
                send_file(OutboundFile(safe_filename(ref.name), ref.mime, raw))
            except (ValueError, OSError, RuntimeError, httpx.HTTPError):
                reply_sender('部分交付文件未能发送，请在 PyCat 的本次产出中查看。请检查机器人权限、平台文件限制，以及文件是否仍存在且未更改。')
                break
