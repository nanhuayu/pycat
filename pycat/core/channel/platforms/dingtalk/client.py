"""DingTalk application robot API; no ephemeral session webhooks in storage."""
import json
import time
from pathlib import Path

import httpx

from pycat.core.channel.media import download_public_media, save_attachment
from pycat.core.channel.replies import fit_reply_text

DINGTALK_API = 'https://api.dingtalk.com'


class DingTalkChannelClient:
    def __init__(self, *, transport=None):
        self._transport = transport
        self._tokens = {}

    def _http(self, timeout=20.0):
        return httpx.Client(timeout=timeout, **({'transport': self._transport} if self._transport else {}))

    @staticmethod
    def _result(response):
        response.raise_for_status()
        data = response.json()
        if data.get('errcode', 0) not in (0, '0') or data.get('code') not in (None, 0, '0'):
            raise RuntimeError('钉钉接口请求失败，请检查应用权限、Robot Code 和消息类型。')
        if data.get('invalidStaffIdList') or data.get('flowControlledStaffIdList'):
            raise RuntimeError('钉钉收件人不存在或发送受到频率限制。')
        return data

    def access_token(self, channel):
        config = channel.config
        key = (str(config.get('client_id') or ''), str(config.get('client_secret') or ''))
        if not all(key):
            raise ValueError('缺少钉钉 Client ID / Client Secret。')
        cached = self._tokens.get(key)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        with self._http() as client:
            data = self._result(client.post(DINGTALK_API + '/v1.0/oauth2/accessToken',
                json={'appKey': key[0], 'appSecret': key[1]}))
        token = str(data.get('accessToken') or '')
        if not token:
            raise RuntimeError('钉钉未返回 Access Token。')
        self._tokens[key] = (token, time.time() + max(0, int(data.get('expireIn') or 7200)))
        return token

    @staticmethod
    def robot_code(channel):
        return str(channel.config.get('robot_code') or channel.config.get('client_id') or '')

    def download_media(self, channel, item, directory):
        if not isinstance(item, dict) or not item.get('download_code'):
            raise ValueError('钉钉附件下载码缺失。')
        token = self.access_token(channel)
        with self._http(60.0) as client:
            data = self._result(client.post(DINGTALK_API + '/v1.0/robot/messageFiles/download',
                headers={'x-acs-dingtalk-access-token': token},
                json={'downloadCode': item['download_code'], 'robotCode': self.robot_code(channel)}))
            raw = download_public_media(client, data.get('downloadUrl'),
                domains=('dingtalk.com', 'dingtalkapps.com', 'alicdn.com', 'aliyuncs.com'))
        return save_attachment(raw, directory, name=item.get('name', ''), image=bool(item.get('image')))

    def _send(self, channel, receive_id, conversation_type, msg_key, params):
        if not receive_id or conversation_type not in {'1', '2'}:
            raise ValueError('钉钉收件人或会话类型无效。')
        payload = {'robotCode': self.robot_code(channel), 'msgKey': msg_key,
            'msgParam': json.dumps(params, ensure_ascii=False)}
        if conversation_type == '2':
            endpoint = '/v1.0/robot/groupMessages/send'
            payload['openConversationId'] = receive_id
        else:
            endpoint = '/v1.0/robot/oToMessages/batchSend'
            payload['userIds'] = [receive_id]
        token = self.access_token(channel)
        with self._http() as client:
            self._result(client.post(DINGTALK_API + endpoint,
                headers={'x-acs-dingtalk-access-token': token}, json=payload))

    def send_text_message(self, channel, *, receive_id, text, conversation_type='1'):
        self._send(channel, receive_id, conversation_type, 'sampleText', {'content': self.normalize_reply_text(text)})

    def send_file(self, channel, *, receive_id, file, conversation_type='1'):
        image = file.mime in {'image/png', 'image/jpeg', 'image/gif', 'image/bmp'} and len(file.data) <= 20 * 1024 * 1024
        token = self.access_token(channel)
        with self._http(60.0) as client:
            result = self._result(client.post('https://oapi.dingtalk.com/media/upload',
                params={'access_token': token, 'type': 'image' if image else 'file', 'robotCode': self.robot_code(channel)},
                files={'media': (file.name, file.data, file.mime)}))
        media_id = result.get('media_id')
        if not media_id:
            raise RuntimeError('钉钉附件上传没有返回媒体标识。')
        self._send(channel, receive_id, conversation_type,
            'sampleImageMsg' if image else 'sampleFile', {'photoURL': media_id} if image else {
                'mediaId': media_id, 'fileName': file.name, 'fileType': Path(file.name).suffix.lstrip('.') or 'file'})

    @staticmethod
    def normalize_reply_text(content, *, limit=4000):
        return fit_reply_text(content, limit)
