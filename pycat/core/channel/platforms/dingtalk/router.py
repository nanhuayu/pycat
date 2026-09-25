import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class DingTalkEnvelope:
    content: str
    meta: dict = field(default_factory=dict)
    media: list[dict] = field(default_factory=list)


def normalize_dingtalk_message(payload, *, mark_recent=None):
    if not isinstance(payload, dict):
        raise ValueError('钉钉消息格式无效。')
    message_id = str(payload.get('msgId') or '')
    conversation_type = str(payload.get('conversationType') or '')
    user = str(payload.get('senderStaffId') or payload.get('senderId') or '')
    conversation = str(payload.get('conversationId') or user)
    if conversation_type not in {'1', '2'} or not user or not conversation or not message_id:
        return None
    kind = str(payload.get('msgtype') or '')
    content = payload.get('content') or {}
    if isinstance(content, str):
        content = json.loads(content)
    if not isinstance(content, dict):
        raise ValueError('钉钉附件描述无效。')
    parts, media = [], []
    if kind == 'text':
        parts.append(str((payload.get('text') or {}).get('content') or '').strip())
    elif kind == 'richText':
        for item in content.get('richText', []):
            if not isinstance(item, dict):
                continue
            if item.get('text'):
                parts.append(str(item['text']))
            if item.get('downloadCode'):
                media.append({'download_code': str(item['downloadCode']), 'name': '', 'image': True})
    elif kind in {'picture', 'file', 'audio', 'video'}:
        if content.get('downloadCode'):
            media.append({'download_code': str(content['downloadCode']),
                'name': str(content.get('fileName') or ''), 'image': kind == 'picture'})
        if content.get('recognition'):
            parts.append(str(content['recognition']))
    text = '\n'.join(parts).strip() or ('[附件]' if media else '')
    if not text or mark_recent and not mark_recent(message_id):
        return None
    return DingTalkEnvelope(text, meta={
        'platform': 'dingtalk', 'message_id': message_id, 'user': user,
        'thread_id': conversation, 'chat_id': conversation,
        'reply_user': conversation if conversation_type == '2' else user,
        'conversation_type': conversation_type,
    }, media=media)
