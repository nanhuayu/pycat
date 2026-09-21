import {node, button, modal, closeModal, reader, toast} from './ui.js';
import {fieldset, fieldControl, choices, categories, option} from './fields.js';
import {resourceBrowser, resourceRow} from './resources.js';

const f = (key, label, type = 'str', extra = {}) => ({key, label, type, ...extra});
const select = values => values.map(([value, label]) => option(value, label));
const providerLabel = provider => ({chatgpt: 'ChatGPT / Codex', workbuddy: 'WorkBuddy / CodeBuddy'})[provider.auth_type] || provider.name;
const models = w => w.draft.providers.flatMap(provider => provider.models.map(model => option(provider.name + '|' + model.model_id, provider.name + ' / ' + (model.display_name || model.model_id))));
const rerender = w => { w.changed(); w.navigate(w.page); };
function remove(w, title, action) {
  modal('删除' + title, node('p', {}, '此更改将在保存设置后生效。'), [button('取消', closeModal), button('从草稿移除', () => { action(); closeModal(); rerender(w); }, 'danger')]);
}
function collection(body, items, selected, choose) {
  const list = node('div', {class: 'resource-list'});
  for (const item of items) list.append(button([
    node('strong', {}, item.name), node('small', {}, item.enabled === false ? '已停用' : item.detail || '')
  ], () => choose(item.id), 'resource-item' + (item.id === selected ? ' active' : '')));
  const detail = node('div', {class: 'resource-detail'});
  body.append(node('div', {class: 'resource-layout'}, list, detail));
  return detail;
}
function required(value, label) { if (!String(value || '').trim()) throw Error(label + '不能为空'); }
function modelEditor(w, provider, item, index) {
  w.edit(index < 0 ? '添加模型' : '模型档案', item, [copy => fieldset('模型', '', copy, [
    f('model_id', '模型 ID'), f('display_name', '显示名称'), f('context_window', '上下文窗口（Token）', 'int', {nullable: true, min: 1}),
    f('max_output_tokens', '最大输出（Token）', 'int', {nullable: true, min: 1}),
    f('supports_tools', '支持工具', 'bool'), f('supports_reasoning', '支持推理', 'bool')
  ]), copy => choices('输入类型', w.schema.input_modalities.map(value => [value, ({text: '文本', image: '图片', audio: '音频'})[value] || value]), copy.input_modalities || ['text'], value => { copy.input_modalities = value; }),
  copy => fieldset('默认生成参数', '留空表示继承。', copy, [
    f('reasoning_codec', '推理参数协议', 'str', {options: provider.catalog_key === 'openrouter' ? ['none', 'chat_reasoning'] : w.schema.reasoning_codecs[provider.api_type]}),
    f('reasoning_options', '可选推理值（每行一项）', 'lines'), f('reasoning_default', '默认推理值'),
    f('default_temperature', 'Temperature', 'float', {nullable: true}), f('default_top_p', 'Top P', 'float', {nullable: true}),
    f('custom_headers', '附加请求头', 'json'), f('extra_body', '附加请求字段', 'json'), f('notes', '备注', 'text')
  ])], copy => {
    required(copy.model_id, '模型 ID');
    if (provider.models.some((item, n) => n !== index && item.model_id === copy.model_id)) throw Error('模型 ID 已存在');
    copy.tags = (copy.tags || []).filter(tag => tag !== 'bundled');
    if (index < 0) provider.models.push(copy); else provider.models[index] = copy;
  });
}
async function account(w, provider, body) {
  const status = await w.api.operation('providers.status', {provider: provider.id});
  const hint = node('p', {class: 'notice'}, status.connected ? '已连接 · ' + (status.email || status.name || provider.name) : '尚未连接账号');
  const actions = node('div', {class: 'toolbar'});
  body.append(hint, actions);
  const connect = button(status.connected ? '退出账号' : '登录账号', event => w.action(async () => {
    if (status.connected) { await w.api.operation('providers.logout', {provider: provider.id}); await w.navigate('models'); return; }
    const login = await w.api.operation('providers.login', {provider: provider.id});
    const job = await w.api.operation('providers.finish-login', {provider: provider.id});
    modal('授权登录', [node('p', {}, '在浏览器完成授权后，本页会更新连接状态。'), node('a', {href: login.url, target: '_blank', rel: 'noopener noreferrer'}, '打开登录页面')],
      [button('取消登录', async () => { await w.api.json('/runs/' + job.run_id + '/cancel', {method: 'POST'}); closeModal(); })]);
    for await (const event of w.api.events(job.run_id)) if (event.type === 'final') {
      closeModal();
      if (event.status !== 'completed') throw Error(event.error || '登录已取消');
      toast('账号已连接'); await w.navigate('models');
    }
  }, event.currentTarget), 'secondary');
  actions.append(connect);
  return status.connected;
}
export async function renderModels(w, body) {
  body.append(fieldset('默认模型', '新会话使用默认对话模型；辅助模型用于压缩、整理等任务。', w.draft.app_settings, [
    f('default_chat_model', '默认对话模型', 'str', {options: [option('', '自动选择'), ...models(w)]}),
    f('default_auxiliary_model', '辅助模型', 'str', {options: [option('', '跟随会话模型'), ...models(w)]})
  ], () => w.changed()));
  const list = w.draft.providers;
  body.append(node('div', {class: 'toolbar'}, node('h3', {}, '服务商'), button('＋ 添加服务商', () => {
    const provider = {id: crypto.randomUUID(), name: 'new-provider', api_type: 'openai_compatible', api_base: '', api_key: '', auth_type: 'api_key', models: [], enabled: false, custom_headers: {}};
    list.push(provider); w.selectedProvider = provider.id; rerender(w);
  })));
  w.selectedProvider = list.some(item => item.id === w.selectedProvider) ? w.selectedProvider : list[0]?.id;
  const detail = collection(body, list.map(item => ({...item, name: providerLabel(item), detail: item.models.length + ' 个模型'})), w.selectedProvider, id => { w.selectedProvider = id; w.navigate('models'); });
  const provider = list.find(item => item.id === w.selectedProvider); if (!provider) return;
  const fixed = ['chatgpt', 'workbuddy'].includes(provider.catalog_key);
  detail.append(node('h3', {}, providerLabel(provider)), fieldset('连接', '', provider, [
    ...(!fixed ? [f('name', '名称', 'str', {help: '字母、数字与连字符，例如 my-provider'})] : []), f('enabled', '启用服务商', 'bool'),
    ...(provider.auth_type === 'api_key' ? [
      f('api_type', '接口类型', 'str', {options: w.schema.api_types}),
      f('api_base', 'API 地址'), f('api_key', 'API Key', 'str', {secret: true})
    ] : [])
  ], () => w.changed()));
  let connected = true;
  if (provider.auth_type !== 'api_key') connected = await account(w, provider, detail);
  const actions = node('div', {class: 'toolbar'});
  const probe = button('测试连接', event => w.action(async () => {
    const result = await w.api.operation('providers.check', {provider: provider.id, configuration: provider}); toast(result[1] || '连接成功');
  }, event.currentTarget));
  const discover = button(provider.auth_type === 'api_key' ? '发现模型' : '同步模型', event => w.action(async () => {
    const available = await w.api.operation('providers.discover', {provider: provider.id, configuration: provider});
    const selected = new Set(provider.models.map(item => item.model_id));
    const search = node('input', {placeholder: '搜索模型', 'aria-label': '搜索模型目录'}), rows = node('div', {class: 'model-discovery'});
    const render = () => rows.replaceChildren(...available.filter(item => (item.model_id + ' ' + item.display_name).toLowerCase().includes(search.value.toLowerCase())).map(item => {
      const input = node('input', {type: 'checkbox'}); input.checked = selected.has(item.model_id);
      input.onchange = () => { if (input.checked) selected.add(item.model_id); else selected.delete(item.model_id); };
      return node('label', {class: 'setting-row'}, input, node('div', {}, node('strong', {}, item.display_name || item.model_id), node('small', {}, item.model_id)));
    }));
    search.oninput = render; render();
    modal('选择模型', [search, rows], [button('取消', closeModal), button('应用到草稿', () => {
      const original = new Map(provider.models.map(item => [item.model_id, item]));
      provider.models = [...selected].map(id => original.get(id) || available.find(item => item.model_id === id)).filter(Boolean);
      closeModal(); rerender(w);
    }, 'primary')]);
  }, event.currentTarget));
  discover.disabled = probe.disabled = !connected; actions.append(probe, discover); detail.append(actions);
  detail.append(node('div', {class: 'toolbar'}, node('h3', {}, '模型档案'), button('＋ 添加模型', event => w.action(async () => {
    const template = await w.api.operation('providers.model-template', {provider: provider.id, configuration: provider});
    modelEditor(w, provider, template, -1);
  }, event.currentTarget))));
  provider.models.forEach((model, index) => detail.append(node('div', {class: 'setting-row'}, node('div', {},
    node('strong', {}, model.display_name || model.model_id), node('small', {}, model.model_id + (model.context_window ? ' · ' + model.context_window.toLocaleString() + ' 上下文' : ''))),
    button('编辑', () => modelEditor(w, provider, model, index)), button('移除', () => { provider.models.splice(index, 1); rerender(w); }))));
  if (!provider.models.length) detail.append(node('p', {class: 'empty-small'}, '暂无模型。连接服务后发现模型，或手动添加。'));
  detail.append(node('details', {}, node('summary', {}, '高级连接字段'),
    fieldset('', '', provider, [f('custom_headers', '附加请求头', 'json')], () => w.changed())));
  if (!fixed) detail.append(button('删除服务商', () => remove(w, provider.name, () => { w.draft.providers = list.filter(item => item.id !== provider.id); }), 'danger text-button'));
}
function targetFields(copy, w) {
  copy.model_target ||= {source: 'auxiliary'};
  return fieldset('模型选择', '子 Agent 和能力可继承主模型、辅助模型或指定模型。', copy, [
    f('model_target.source', '模型来源', 'str', {options: select([['primary', '主模型'], ['auxiliary', '辅助模型'], ['explicit', '指定模型']])}),
    f('model_target.model_ref', '指定模型', 'str', {options: [option('', '选择模型'), ...models(w)]})
  ]);
}
export function renderModes(w, body) {
  const list = w.draft.modes;
  function edit(item, index) {
    w.edit(index < 0 ? '添加模式 / Agent' : item.name, item, [
      copy => fieldset('基本信息', '', copy, [f('slug', '标识'), f('name', '名称'), f('purpose', '用途'), f('profile_kind', '类型', 'str', {options: select([['primary', '主模式'], ['subagent', '子 Agent'], ['both', '两者均可']])}),
        f('completion_policy', '完成方式', 'str', {options: select([['text', '文本回复'], ['explicit', '显式完成']])}), f('prompt', '指令', 'text')]),
      copy => choices('可用工具类别', categories, copy.allowed_tool_categories || [], value => { copy.allowed_tool_categories = value; }),
      copy => targetFields(copy, w),
      copy => fieldset('子 Agent 策略', '', copy, [f('max_turns', '最多轮数', 'int', {nullable: true, min: 1}),
        f('shared_context_policy', '共享上下文', 'str', {options: select([['indexes_only', '只共享索引'], ['selected_artifacts', '指定成果'], ['full_session_readonly', '只读会话']])})])
    ], copy => {
      required(copy.slug, '标识'); required(copy.name, '名称');
      if (list.some((item, n) => n !== index && item.slug === copy.slug)) throw Error('标识已存在');
      if (index < 0) list.push(copy); else list[index] = copy;
    });
  }
  body.append(node('div', {class: 'toolbar'}, node('p', {}, '主模式决定当前会话；子 Agent 由 /agents run 或工具显式执行。'), button('＋ 添加', () => edit({slug: '', name: '', profile_kind: 'subagent', completion_policy: 'explicit', prompt: '', allowed_tool_categories: ['read'], model_target: {source: 'auxiliary'}, shared_context_policy: 'indexes_only'}, -1))));
  list.forEach((item, index) => body.append(node('div', {class: 'setting-row'}, node('div', {}, node('strong', {}, item.name), node('small', {}, ({primary: '主模式', subagent: '子 Agent', both: '主模式与子 Agent'}[item.profile_kind]) + ' · ' + (item.purpose || item.slug))),
    button('编辑', () => edit(item, index)), item.source !== 'builtin' ? button('删除', () => remove(w, item.name, () => list.splice(index, 1))) : null)));
}
export function renderMcp(w, body) {
  const list = w.draft.mcp_servers;
  const edit = (item, index) => w.edit(index < 0 ? '添加 MCP 服务' : item.name, item, [
    copy => fieldset('服务', '', copy, [f('name', '名称'), f('enabled', '启用', 'bool'),
      f('transport', '传输方式', 'str', {options: ['stdio', 'streamable_http', 'sse']})]),
    copy => {
      const group = node('div'), update = () => {
        group.replaceChildren(fieldset(copy.transport === 'stdio' ? '本地进程' : '远程连接', '', copy, copy.transport === 'stdio'
          ? [f('command', '启动程序'), f('args', '启动参数（每行一项）', 'lines'), f('cwd', '工作目录'), f('env', '环境变量', 'json')]
          : [f('url', '服务地址'), f('headers', '请求头', 'json')]));
      };
      update(); queueMicrotask(() => group.closest('.resource-editor')?.querySelector('select')?.addEventListener('change', update)); return group;
    }
  ], async copy => {
    required(copy.name, '名称');
    if (list.some((value, position) => position !== index && value.name === copy.name)) throw Error('服务名称已存在');
    const validated = await w.api.operation('mcp.validate', {configuration: copy});
    if (index < 0) list.push(validated); else list[index] = validated;
  });
  body.append(node('div', {class: 'toolbar'},
    button('＋ 添加 MCP', () => edit({name: '', transport: 'stdio', command: '', args: [], env: {}, headers: {}, enabled: false}, -1)),
    button('导入 mcp.json', () => {
      const input = node('input', {type: 'file', accept: '.json'});
      input.onchange = () => w.action(async () => {
        const file = input.files[0]; if (!file) return;
        if (file.size > 1024 * 1024) throw Error('配置文件超过 1 MB');
        const result = await w.api.operation('mcp.import', {configuration: JSON.parse(await file.text()), existing_names: list.map(item => item.name)});
        list.push(...result.servers); rerender(w);
        modal('导入 MCP 草稿', node('div', {}, node('p', {}, '已导入 ' + result.servers.length + ' 个服务，默认停用。保存后生效。'),
          result.errors.map(error => node('p', {class: 'form-error'}, error))), [button('关闭', closeModal)]);
      });
      input.click();
    }),
    button('导出 mcp.json', event => w.action(async () => {
      const value = await w.api.operation('mcp.export', {servers: list});
      const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], {type: 'application/json'}));
      const link = node('a', {href: url, download: 'mcp.json'}); link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast('已导出启用的服务，包含其环境变量和请求头');
    }, event.currentTarget))));
  const search = node('input', {placeholder: '搜索已安装 MCP', 'aria-label': '搜索已安装 MCP'});
  const rows = node('div', {class: 'resource-list'}), detail = node('div', {class: 'resource-detail'});
  const browser = resourceBrowser(rows, detail);
  let active = null;
  const render = () => rows.replaceChildren(...list.filter(item => (item.name + ' ' + item.command + ' ' + item.url).toLowerCase().includes(search.value.toLowerCase())).map(item => resourceRow({
    title: item.name, description: item.command || item.url || '尚未配置', meta: item.transport + ' · ' + (item.enabled ? '已启用' : '已停用'),
    enabled: item.enabled, toggle: () => { item.enabled = !item.enabled; w.changed(); render(); if (active === item) open(item); }, open: () => open(item)})));
  function open(item, reveal = true) {
    const invalid = detail.querySelector(':invalid');
    if (invalid) { invalid.reportValidity(); return; }
    active = item;
    browser.open(item.name, reveal);
    const fields = node('div', {class: 'resource-form'}), tools = node('div');
    const update = () => { w.changed(); render(); };
    function connectionFields() {
      fields.replaceChildren(fieldset('连接', '', item, item.transport === 'stdio'
        ? [f('command', '命令'), f('args', '参数（每行一项）', 'lines'), f('cwd', '工作目录'), f('env', '环境变量', 'json')]
        : [f('url', '服务 URL'), f('headers', '请求头', 'json')], update));
    }
    function showTools(schemas = []) {
      const names = item.cached_tools || [], find = node('input', {placeholder: '搜索工具', 'aria-label': '搜索工具'});
      const catalog = node('div', {class: 'resource-tool-list'});
      const filter = () => catalog.replaceChildren(...names.filter(name => name.toLowerCase().includes(find.value.toLowerCase())).map(name => {
        const schema = schemas.find(value => value.name === name);
        return node('details', {}, node('summary', {}, name), node('p', {}, schema?.description || '测试连接后可查看工具说明和参数。'),
          schema ? node('pre', {}, JSON.stringify(schema.inputSchema || {}, null, 2)) : null);
      }));
      find.oninput = filter; filter();
      tools.replaceChildren(node('details', {class: 'resource-tools', open: ''}, node('summary', {}, '工具 · ' + names.length), find, catalog));
    }
    const test = button('测试连接', event => w.action(async () => {
      const invalid = detail.querySelector(':invalid'); if (invalid) { invalid.reportValidity(); return; }
      const before = JSON.stringify(item);
      const result = await w.api.operation('mcp.probe', {name: item.name, configuration: structuredClone(item)});
      if (!detail.isConnected || active !== item || JSON.stringify(item) !== before) return;
      if (!result.ok) throw Error(result.error || '连接失败');
      item.cached_tools = result.tools; showTools(result.schemas || []); w.changed(); toast('已连接，发现 ' + result.tools.length + ' 个工具');
    }, event.currentTarget));
    detail.replaceChildren(node('h2', {}, item.name), node('div', {class: 'toolbar'},
      button(item.enabled ? '停用' : '启用', () => { item.enabled = !item.enabled; update(); open(item); }), test,
      button('版本与更新', () => w.showResourceUpdate(item.integration === 'agent-browser' ? 'agent-browser' : 'configured:' + item.name)),
      button('删除', () => remove(w, item.name, () => list.splice(list.indexOf(item), 1)), 'text-button')),
      node('div', {class: 'resource-form'}, fieldset('基本信息', '', item, [f('name', '名称'), f('transport', '传输方式', 'str', {options: ['stdio', 'streamable_http', 'sse']})], key => {
        if (key === 'transport') connectionFields(); update();
      })), fields, tools);
    connectionFields(); showTools();
  }
  search.oninput = render;
  body.prepend(search); body.append(browser.element);
  render(); if (list.length) open(list[0], false);
  else rows.append(node('p', {class: 'empty-small'}, '没有 MCP 服务。可以从市场添加或手动配置。'));

}
export function renderCapabilities(w, body) {
  const defaults = w.schema.capabilities.capabilities;
  const overrides = w.draft.app_settings.capabilities.capabilities;
  const list = [...new Map([...defaults, ...overrides].map(item => [item.id, structuredClone(item)])).values()];
  const commit = () => { w.draft.app_settings.capabilities.capabilities = list; rerender(w); };
  const edit = (item, index) => w.edit(index < 0 ? '添加能力' : item.name, item, [
    copy => fieldset('能力', '', copy, [
      f('id', '标识'), f('name', '名称'), f('description', '说明'),
      f('exposure', '使用方式', 'str', {options: select([['tool', '模型工具'], ['internal', '仅内部']])}),
      f('runtime', '执行方式', 'str', {options: select([['single_turn', '单轮转换'], ['agent_loop', '受限 Agent 循环']])}),
      f('prompt', '指令', 'text')
    ]),
    copy => targetFields(copy, w),
    copy => choices('可用工具类别', categories, copy.allowed_tool_categories || [], value => { copy.allowed_tool_categories = value; }),
    copy => fieldset('生成参数与数据格式', '', copy, [
      f('temperature', 'Temperature', 'float', {nullable: true}), f('max_tokens', '最大输出', 'int', {nullable: true}),
      f('max_turns', '最多轮数', 'int', {nullable: true}), f('input_schema', '输入 Schema', 'json'), f('output_schema', '输出 Schema', 'json')
    ])
  ], copy => {
    required(copy.id, '标识'); required(copy.name, '名称');
    if (index < 0 && list.some(value => value.id === copy.id)) throw Error('能力标识已存在');
    if (index >= 0 && defaults.some(value => value.id === item.id) && copy.id !== item.id) throw Error('内置能力不能修改标识');
    if (index < 0) list.push(copy); else list[index] = copy;
    w.draft.app_settings.capabilities.capabilities = list;
  });
  body.append(node('div', {class: 'toolbar'}, node('p', {}, '能力可以进行单轮转换或受限 Agent 执行。'), button('＋ 添加能力', () => edit({
    id: '', name: '', enabled: true, exposure: 'tool', runtime: 'single_turn', model_target: {source: 'auxiliary'},
    description: '', prompt: '', input_schema: {}, output_schema: {}, allowed_tool_categories: [], max_turns: null, temperature: null, max_tokens: null
  }, -1))));
  for (const [index, item] of list.entries()) body.append(node('div', {class: 'setting-row'}, node('div', {}, node('strong', {}, item.name), node('small', {}, item.description)),
    button(item.enabled ? '停用' : '启用', () => { item.enabled = !item.enabled; commit(); }),
    button('配置', () => edit(item, index)),
    defaults.some(value => value.id === item.id) ? null : button('删除', () => remove(w, item.name, () => { list.splice(index, 1); w.draft.app_settings.capabilities.capabilities = list; }))));
}
