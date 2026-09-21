import {node, button, modal, closeModal, toast} from './ui.js';
import {fieldset, option} from './fields.js';
import {showConfig} from './config.js';
export const showChannels = (api, refresh) => showConfig(api, refresh, 'channels');

async function login(w, channel) {
  // Login credentials are immediate; configuration changes must be committed first.
  if (w.dirty.length) { toast('请先保存配置，再开始扫码登录'); return; }
  const result = await w.api.operation('channels.login', {channel: channel.id});
  const qr = await w.api.request('/channels/login/' + result.id + '/qr');
  const url = URL.createObjectURL(await qr.blob());
  const status = node('p', {}, result.snapshot.detail || '使用微信扫描二维码');
  const code = node('input', {placeholder: '如需验证，请填写验证码', 'aria-label': '验证码'});
  const cleanup = () => URL.revokeObjectURL(url);
  document.querySelector('#dialog').addEventListener('close', cleanup, {once: true});
  modal('连接微信', [node('img', {src: url, alt: '微信登录二维码', width: 220, height: 220}), status, code], [
    button('关闭', closeModal), button('刷新登录状态', event => w.action(async () => {
      const value = await w.api.operation('channels.poll-login', {login: result.id, verification_code: code.value});
      status.textContent = value.snapshot.detail;
      if (value.complete) {
        cleanup(); closeModal();
        w.snapshot = await w.api.operation('config.read'); w.draft = structuredClone(w.snapshot.values);
        toast('微信已连接，可启用频道接收消息'); await w.navigate('channels');
      }
    }, event.currentTarget), 'primary')
  ]);
}
export async function renderChannels(w, body) {
  const [definitions, states, sessions] = await Promise.all([
    w.api.operation('channels.types'), w.api.operation('channels.list'), w.api.operation('sessions.list', {limit: 200})
  ]);
  const list = w.draft.app_settings.channels;
  const edit = (channel, index) => {
    const definition = definitions.find(item => item.type === channel.type);
    const declared = (definition?.fields || []);
    w.edit(channel.name || '新频道', channel, [
      copy => fieldset('消息连接', definition?.description || '', copy, [
        {key: 'name', label: '名称'}, {key: 'enabled', label: '启用', type: 'bool'},
        {key: 'session_id', label: '绑定会话', options: [option('', '创建独立会话'), ...sessions.map(item => option(item.id, item.title || '未命名会话'))]},
        {key: 'mode_slug', label: '运行模式', options: w.draft.modes.filter(item => item.profile_kind !== 'subagent').map(item => option(item.slug, item.name))}
      ]),
      copy => fieldset('凭据与接入', '', copy.config ||= {}, declared.map(item => ({key: item.key, label: item.label, secret: item.secret, help: item.help_text}))),
      copy => {
        const advanced = Object.fromEntries(Object.entries(copy.config).filter(([key]) => !declared.some(field => field.key === key)));
        return node('details', {}, node('summary', {}, '高级连接选项'), fieldset('', '', {value: advanced},
          [{key: 'value', label: '连接参数', type: 'json'}], (_, value) => { copy.config = {...value, ...Object.fromEntries(declared.map(field => [field.key, copy.config[field.key]]))}; }));
      }
    ], copy => { if (!copy.name.trim()) throw Error('名称不能为空'); if (index < 0) list.push(copy); else list[index] = copy; });
  };
  const type = node('select', {'aria-label': '频道类型'}, definitions.map(item => node('option', {value: item.type}, item.name)));
  body.append(node('div', {class: 'toolbar'}, type, button('＋ 添加频道', event => w.action(async () => {
    const item = await w.api.operation('channels.create', {type: type.value}); item.enabled = false; edit(item, -1);
  }, event.currentTarget))));
  for (const [index, channel] of list.entries()) {
    const connection = states.find(item => item.channel.id === channel.id)?.connection;
    body.append(node('div', {class: 'setting-row'}, node('div', {}, node('strong', {}, channel.name),
      node('small', {}, (channel.enabled ? '启用' : '停用') + ' · ' + (connection?.detail || channel.type))),
      channel.type === 'wechat' ? button('扫码登录', event => w.action(() => login(w, channel), event.currentTarget)) : null,
      button('编辑', () => edit(channel, index)), button('删除', () => modal('删除频道', node('p', {}, '保存设置后移除此连接。'),
        [button('取消', closeModal), button('从草稿移除', () => { list.splice(index, 1); closeModal(); w.changed(); w.navigate('channels'); }, 'danger')]))));
  }
  if (!list.length) body.append(node('p', {class: 'empty-small'}, '尚未添加消息通道。选择平台后填写连接信息。'));
}
