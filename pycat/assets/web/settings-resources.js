import {tr} from './i18n.js';
import {node, button, modal, closeModal, reader, toast} from './ui.js';
import {fieldset, fieldControl, choices, categories, option} from './fields.js';
import {resourceBrowser, resourceRow} from './resources.js';

const f = (key, label, type = 'str', extra = {}) => ({key, label, type, ...extra});
const select = values => values.map(([value, label]) => option(value, label));
const providerLabel = provider => ({chatgpt: 'ChatGPT / Codex', workbuddy: 'WorkBuddy / CodeBuddy'})[provider.auth_type] || provider.name;
const models = w => w.draft.providers.flatMap(provider => provider.models.map(model => option(provider.name + '|' + model.model_id, provider.name + ' / ' + (model.display_name || model.model_id))));
const rerender = w => { w.changed(); w.navigate(w.page); };
function remove(w, title, action) {
  modal(tr("删除") + title, node('p', {}, tr("此更改将在保存设置后生效。")), [button(tr("取消"), closeModal), button(tr("从草稿移除"), () => { action(); closeModal(); rerender(w); }, 'danger')]);
}
function collection(body, items, selected, choose) {
  const list = node('div', {class: 'resource-list'});
  for (const item of items) list.append(button([
    node('strong', {}, item.name), node('small', {}, item.enabled === false ? tr("已停用") : item.detail || '')
  ], () => choose(item.id), 'resource-item' + (item.id === selected ? ' active' : '')));
  const detail = node('div', {class: 'resource-detail'});
  body.append(node('div', {class: 'resource-layout'}, list, detail));
  return detail;
}
function required(value, label) { if (!String(value || '').trim()) throw Error(label + tr("不能为空")); }
function modelEditor(w, provider, item, index) {
  w.edit(index < 0 ? tr("添加模型") : tr("模型档案"), item, [copy => fieldset(tr("模型"), '', copy, [
    f('model_id', tr("模型 ID")), f('display_name', tr("显示名称")), f('context_window', tr("上下文窗口（Token）"), 'int', {nullable: true, min: 1}),
    f('max_output_tokens', tr("最大输出（Token）"), 'int', {nullable: true, min: 1}),
    f('supports_tools', tr("支持工具"), 'bool'), f('supports_reasoning', tr("支持推理"), 'bool')
  ]), copy => choices(tr("输入类型"), w.schema.input_modalities.map(value => [value, ({text: tr("文本"), image: tr("图片"), audio: tr("音频")})[value] || value]), copy.input_modalities || ['text'], value => { copy.input_modalities = value; }),
  copy => fieldset(tr("默认生成参数"), tr("留空表示继承。"), copy, [
    f('reasoning_codec', tr("推理参数协议"), 'str', {options: provider.catalog_key === 'openrouter' ? ['none', 'chat_reasoning'] : w.schema.reasoning_codecs[provider.api_type]}),
    f('reasoning_options', tr("可选推理值（每行一项）"), 'lines'), f('reasoning_default', tr("默认推理值")),
    f('default_temperature', 'Temperature', 'float', {nullable: true}), f('default_top_p', 'Top P', 'float', {nullable: true}),
    f('custom_headers', tr("附加请求头"), 'json'), f('extra_body', tr("附加请求字段"), 'json'), f('notes', tr("备注"), 'text')
  ])], copy => {
    required(copy.model_id, tr("模型 ID"));
    if (provider.models.some((item, n) => n !== index && item.model_id === copy.model_id)) throw Error(tr("模型 ID 已存在"));
    copy.tags = (copy.tags || []).filter(tag => tag !== 'bundled');
    if (index < 0) provider.models.push(copy); else provider.models[index] = copy;
  });
}
async function account(w, provider, body) {
  const status = await w.api.operation('providers.status', {provider: provider.id});
  const hint = node('p', {class: 'notice'}, status.connected ? tr("已连接 · ") + (status.email || status.name || provider.name) : tr("尚未连接账号"));
  const actions = node('div', {class: 'toolbar'});
  body.append(hint, actions);
  const connect = button(status.connected ? tr("退出账号") : tr("登录账号"), event => w.action(async () => {
    if (status.connected) { await w.api.operation('providers.logout', {provider: provider.id}); await w.navigate('models'); return; }
    const login = await w.api.operation('providers.login', {provider: provider.id});
    const job = await w.api.operation('providers.finish-login', {provider: provider.id});
    modal(tr("授权登录"), [node('p', {}, tr("在浏览器完成授权后，本页会更新连接状态。")), node('a', {href: login.url, target: '_blank', rel: 'noopener noreferrer'}, tr("打开登录页面"))],
      [button(tr("取消登录"), async () => { await w.api.json('/runs/' + job.run_id + '/cancel', {method: 'POST'}); closeModal(); })]);
    for await (const event of w.api.events(job.run_id)) if (event.type === 'final') {
      closeModal();
      if (event.status !== 'completed') throw Error(event.error || tr("登录已取消"));
      toast(tr("账号已连接")); await w.navigate('models');
    }
  }, event.currentTarget), 'secondary');
  actions.append(connect);
  return status.connected;
}
export async function renderModels(w, body) {
  body.append(fieldset(tr("默认模型"), tr("新会话使用默认对话模型；辅助模型用于压缩、整理等任务。"), w.draft.app_settings, [
    f('default_chat_model', tr("默认对话模型"), 'str', {options: [option('', tr("自动选择")), ...models(w)]}),
    f('default_auxiliary_model', tr("辅助模型"), 'str', {options: [option('', tr("跟随会话模型")), ...models(w)]})
  ], () => w.changed()));
  const list = w.draft.providers;
  body.append(node('div', {class: 'toolbar'}, node('h3', {}, tr("服务商")), button(tr("＋ 添加服务商"), () => {
    const provider = {id: crypto.randomUUID(), name: 'new-provider', api_type: 'openai_compatible', api_base: '', api_key: '', auth_type: 'api_key', models: [], enabled: false, custom_headers: {}};
    list.push(provider); w.selectedProvider = provider.id; rerender(w);
  })));
  w.selectedProvider = list.some(item => item.id === w.selectedProvider) ? w.selectedProvider : list[0]?.id;
  const detail = collection(body, list.map(item => ({...item, name: providerLabel(item), detail: item.models.length + tr(" 个模型")})), w.selectedProvider, id => { w.selectedProvider = id; w.navigate('models'); });
  const provider = list.find(item => item.id === w.selectedProvider); if (!provider) return;
  const fixed = ['chatgpt', 'workbuddy'].includes(provider.catalog_key);
  detail.append(node('h3', {}, providerLabel(provider)), fieldset(tr("连接"), '', provider, [
    ...(!fixed ? [f('name', tr("名称"), 'str', {help: tr("字母、数字与连字符，例如 my-provider")})] : []), f('enabled', tr("启用服务商"), 'bool'),
    ...(provider.auth_type === 'api_key' ? [
      f('api_type', tr("接口类型"), 'str', {options: w.schema.api_types}),
      f('api_base', tr("API 地址")), f('api_key', 'API Key', 'str', {secret: true})
    ] : [])
  ], () => w.changed()));
  let connected = true;
  if (provider.auth_type !== 'api_key') connected = await account(w, provider, detail);
  const actions = node('div', {class: 'toolbar'});
  const probe = button(tr("测试连接"), event => w.action(async () => {
    const result = await w.api.operation('providers.check', {provider: provider.id, configuration: provider}); toast(result[1] || tr("连接成功"));
  }, event.currentTarget));
  const discover = button(provider.auth_type === 'api_key' ? tr("发现模型") : tr("同步模型"), event => w.action(async () => {
    const available = await w.api.operation('providers.discover', {provider: provider.id, configuration: provider});
    const selected = new Set(provider.models.map(item => item.model_id));
    const search = node('input', {placeholder: tr("搜索模型"), 'aria-label': tr("搜索模型目录")}), rows = node('div', {class: 'model-discovery'});
    const render = () => rows.replaceChildren(...available.filter(item => (item.model_id + ' ' + item.display_name).toLowerCase().includes(search.value.toLowerCase())).map(item => {
      const input = node('input', {type: 'checkbox'}); input.checked = selected.has(item.model_id);
      input.onchange = () => { if (input.checked) selected.add(item.model_id); else selected.delete(item.model_id); };
      return node('label', {class: 'setting-row'}, input, node('div', {}, node('strong', {}, item.display_name || item.model_id), node('small', {}, item.model_id)));
    }));
    search.oninput = render; render();
    modal(tr("选择模型"), [search, rows], [button(tr("取消"), closeModal), button(tr("应用到草稿"), () => {
      const original = new Map(provider.models.map(item => [item.model_id, item]));
      provider.models = [...selected].map(id => original.get(id) || available.find(item => item.model_id === id)).filter(Boolean);
      closeModal(); rerender(w);
    }, 'primary')]);
  }, event.currentTarget));
  discover.disabled = probe.disabled = !connected; actions.append(probe, discover); detail.append(actions);
  detail.append(node('div', {class: 'toolbar'}, node('h3', {}, tr("模型档案")), button(tr("＋ 添加模型"), event => w.action(async () => {
    const template = await w.api.operation('providers.model-template', {provider: provider.id, configuration: provider});
    modelEditor(w, provider, template, -1);
  }, event.currentTarget))));
  provider.models.forEach((model, index) => detail.append(node('div', {class: 'setting-row'}, node('div', {},
    node('strong', {}, model.display_name || model.model_id), node('small', {}, model.model_id + (model.context_window ? ' · ' + model.context_window.toLocaleString() + tr(" 上下文") : ''))),
    button(tr("编辑"), () => modelEditor(w, provider, model, index)), button(tr("移除"), () => { provider.models.splice(index, 1); rerender(w); }))));
  if (!provider.models.length) detail.append(node('p', {class: 'empty-small'}, tr("暂无模型。连接服务后发现模型，或手动添加。")));
  detail.append(node('details', {}, node('summary', {}, tr("高级连接字段")),
    fieldset('', '', provider, [f('custom_headers', tr("附加请求头"), 'json')], () => w.changed())));
  if (!fixed) detail.append(button(tr("删除服务商"), () => remove(w, provider.name, () => { w.draft.providers = list.filter(item => item.id !== provider.id); }), 'danger text-button'));
}
function targetFields(copy, w) {
  copy.model_target ||= {source: 'auxiliary'};
  return fieldset(tr("模型选择"), tr("子 Agent 和能力可继承主模型、辅助模型或指定模型。"), copy, [
    f('model_target.source', tr("模型来源"), 'str', {options: select([['primary', tr("主模型")], ['auxiliary', tr("辅助模型")], ['explicit', tr("指定模型")]])}),
    f('model_target.model_ref', tr("指定模型"), 'str', {options: [option('', tr("选择模型")), ...models(w)]})
  ]);
}
export function renderModes(w, body) {
  const list = w.draft.modes;
  function edit(item, index) {
    w.edit(index < 0 ? tr("添加模式 / Agent") : item.name, item, [
      copy => fieldset(tr("基本信息"), '', copy, [f('slug', tr("标识")), f('name', tr("名称")), f('purpose', tr("用途")), f('profile_kind', tr("类型"), 'str', {options: select([['primary', tr("主模式")], ['subagent', tr("子 Agent")], ['both', tr("两者均可")]])}),
        f('completion_policy', tr("完成方式"), 'str', {options: select([['text', tr("文本回复")], ['explicit', tr("显式完成")]])}), f('prompt', tr("指令"), 'text')]),
      copy => choices(tr("可用工具类别"), categories, copy.allowed_tool_categories || [], value => { copy.allowed_tool_categories = value; }),
      copy => targetFields(copy, w),
      copy => fieldset(tr("子 Agent 策略"), '', copy, [f('max_turns', tr("最多轮数"), 'int', {nullable: true, min: 1}),
        f('shared_context_policy', tr("共享上下文"), 'str', {options: select([['indexes_only', tr("只共享索引")], ['selected_artifacts', tr("指定成果")], ['full_session_readonly', tr("只读会话")]])})])
    ], copy => {
      required(copy.slug, tr("标识")); required(copy.name, tr("名称"));
      if (list.some((item, n) => n !== index && item.slug === copy.slug)) throw Error(tr("标识已存在"));
      if (index < 0) list.push(copy); else list[index] = copy;
    });
  }
  body.append(node('div', {class: 'toolbar'}, node('p', {}, tr("主模式决定当前会话；子 Agent 由 /agents run 或工具显式执行。")), button(tr("＋ 添加"), () => edit({slug: '', name: '', profile_kind: 'subagent', completion_policy: 'explicit', prompt: '', allowed_tool_categories: ['read'], model_target: {source: 'auxiliary'}, shared_context_policy: 'indexes_only'}, -1))));
  list.forEach((item, index) => body.append(node('div', {class: 'setting-row'}, node('div', {}, node('strong', {}, item.name), node('small', {}, ({primary: tr("主模式"), subagent: tr("子 Agent"), both: tr("主模式与子 Agent")}[item.profile_kind]) + ' · ' + (item.purpose || item.slug))),
    button(tr("编辑"), () => edit(item, index)), item.source !== 'builtin' ? button(tr("删除"), () => remove(w, item.name, () => list.splice(index, 1))) : null)));
}
export function renderMcp(w, body) {
  const list = w.draft.mcp_servers;
  const edit = (item, index) => w.edit(index < 0 ? tr("添加 MCP 服务") : item.name, item, [
    copy => fieldset(tr("服务"), '', copy, [f('name', tr("名称")), f('enabled', tr("启用"), 'bool'),
      f('transport', tr("传输方式"), 'str', {options: ['stdio', 'streamable_http', 'sse']})]),
    copy => {
      const group = node('div'), update = () => {
        group.replaceChildren(fieldset(copy.transport === 'stdio' ? tr("本地进程") : tr("远程连接"), '', copy, copy.transport === 'stdio'
          ? [f('command', tr("启动程序")), f('args', tr("启动参数（每行一项）"), 'lines'), f('cwd', tr("工作目录")), f('env', tr("环境变量"), 'json')]
          : [f('url', tr("服务地址")), f('headers', tr("请求头"), 'json')]));
      };
      update(); queueMicrotask(() => group.closest('.resource-editor')?.querySelector('select')?.addEventListener('change', update)); return group;
    }
  ], async copy => {
    required(copy.name, tr("名称"));
    if (list.some((value, position) => position !== index && value.name === copy.name)) throw Error(tr("服务名称已存在"));
    const validated = await w.api.operation('mcp.validate', {configuration: copy});
    if (index < 0) list.push(validated); else list[index] = validated;
  });
  body.append(node('div', {class: 'toolbar'},
    button(tr("＋ 添加 MCP"), () => edit({name: '', transport: 'stdio', command: '', args: [], env: {}, headers: {}, enabled: false}, -1)),
    button(tr("导入 mcp.json"), () => {
      const input = node('input', {type: 'file', accept: '.json'});
      input.onchange = () => w.action(async () => {
        const file = input.files[0]; if (!file) return;
        if (file.size > 1024 * 1024) throw Error(tr("配置文件超过 1 MB"));
        const result = await w.api.operation('mcp.import', {configuration: JSON.parse(await file.text()), existing_names: list.map(item => item.name)});
        list.push(...result.servers); rerender(w);
        modal(tr("导入 MCP 草稿"), node('div', {}, node('p', {}, tr("已导入 ") + result.servers.length + tr(" 个服务，默认停用。保存后生效。")),
          result.errors.map(error => node('p', {class: 'form-error'}, error))), [button(tr("关闭"), closeModal)]);
      });
      input.click();
    }),
    button(tr("导出 mcp.json"), event => w.action(async () => {
      const value = await w.api.operation('mcp.export', {servers: list});
      const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], {type: 'application/json'}));
      const link = node('a', {href: url, download: 'mcp.json'}); link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast(tr("已导出启用的服务，包含其环境变量和请求头"));
    }, event.currentTarget))));
  const search = node('input', {placeholder: tr("搜索已安装 MCP"), 'aria-label': tr("搜索已安装 MCP")});
  const rows = node('div', {class: 'resource-list'}), detail = node('div', {class: 'resource-detail'});
  const browser = resourceBrowser(rows, detail);
  let active = null;
  const render = () => rows.replaceChildren(...list.filter(item => (item.name + ' ' + item.command + ' ' + item.url).toLowerCase().includes(search.value.toLowerCase())).map(item => resourceRow({
    title: item.name, description: item.command || item.url || tr("尚未配置"), meta: item.transport + ' · ' + (item.enabled ? tr("已启用") : tr("已停用")),
    enabled: item.enabled, toggle: () => { item.enabled = !item.enabled; w.changed(); render(); if (active === item) open(item); }, open: () => open(item)})));
  function open(item, reveal = true) {
    const invalid = detail.querySelector(':invalid');
    if (invalid) { invalid.reportValidity(); return; }
    active = item;
    browser.open(item.name, reveal);
    const fields = node('div', {class: 'resource-form'}), tools = node('div');
    const update = () => { w.changed(); render(); };
    function connectionFields() {
      fields.replaceChildren(fieldset(tr("连接"), '', item, item.transport === 'stdio'
        ? [f('command', tr("命令")), f('args', tr("参数（每行一项）"), 'lines'), f('cwd', tr("工作目录")), f('env', tr("环境变量"), 'json')]
        : [f('url', tr("服务 URL")), f('headers', tr("请求头"), 'json')], update));
    }
    function showTools(schemas = []) {
      const names = item.cached_tools || [], find = node('input', {placeholder: tr("搜索工具"), 'aria-label': tr("搜索工具")});
      const catalog = node('div', {class: 'resource-tool-list'});
      const filter = () => catalog.replaceChildren(...names.filter(name => name.toLowerCase().includes(find.value.toLowerCase())).map(name => {
        const schema = schemas.find(value => value.name === name);
        return node('details', {}, node('summary', {}, name), node('p', {}, schema?.description || tr("测试连接后可查看工具说明和参数。")),
          schema ? node('pre', {}, JSON.stringify(schema.inputSchema || {}, null, 2)) : null);
      }));
      find.oninput = filter; filter();
      tools.replaceChildren(node('details', {class: 'resource-tools', open: ''}, node('summary', {}, tr("工具 · ") + names.length), find, catalog));
    }
    const test = button(tr("测试连接"), event => w.action(async () => {
      const invalid = detail.querySelector(':invalid'); if (invalid) { invalid.reportValidity(); return; }
      const before = JSON.stringify(item);
      const result = await w.api.operation('mcp.probe', {name: item.name, configuration: structuredClone(item)});
      if (!detail.isConnected || active !== item || JSON.stringify(item) !== before) return;
      if (!result.ok) throw Error(result.error || tr("连接失败"));
      item.cached_tools = result.tools; showTools(result.schemas || []); w.changed(); toast(tr("已连接，发现 ") + result.tools.length + tr(" 个工具"));
    }, event.currentTarget));
    detail.replaceChildren(node('h2', {}, item.name), node('div', {class: 'toolbar'},
      button(item.enabled ? tr("停用") : tr("启用"), () => { item.enabled = !item.enabled; update(); open(item); }), test,
      button(tr("版本与更新"), () => w.showResourceUpdate(item.integration === 'agent-browser' ? 'agent-browser' : 'configured:' + item.name)),
      button(tr("删除"), () => remove(w, item.name, () => list.splice(list.indexOf(item), 1)), 'text-button')),
      node('div', {class: 'resource-form'}, fieldset(tr("基本信息"), '', item, [f('name', tr("名称")), f('transport', tr("传输方式"), 'str', {options: ['stdio', 'streamable_http', 'sse']})], key => {
        if (key === 'transport') connectionFields(); update();
      })), fields, tools);
    connectionFields(); showTools();
  }
  search.oninput = render;
  body.prepend(search); body.append(browser.element);
  render(); if (list.length) open(list[0], false);
  else rows.append(node('p', {class: 'empty-small'}, tr("没有 MCP 服务。可以从市场添加或手动配置。")));

}
export function renderCapabilities(w, body) {
  const defaults = w.schema.capabilities.capabilities;
  const overrides = w.draft.app_settings.capabilities.capabilities;
  const list = [...new Map([...defaults, ...overrides].map(item => [item.id, structuredClone(item)])).values()];
  const commit = () => { w.draft.app_settings.capabilities.capabilities = list; rerender(w); };
  const edit = (item, index) => w.edit(index < 0 ? tr("添加能力") : item.name, item, [
    copy => fieldset(tr("能力"), '', copy, [
      f('id', tr("标识")), f('name', tr("名称")), f('description', tr("说明")),
      f('exposure', tr("使用方式"), 'str', {options: select([['tool', tr("模型工具")], ['internal', tr("仅内部")]])}),
      f('runtime', tr("执行方式"), 'str', {options: select([['single_turn', tr("单轮转换")], ['agent_loop', tr("受限 Agent 循环")]])}),
      f('prompt', tr("指令"), 'text')
    ]),
    copy => targetFields(copy, w),
    copy => choices(tr("可用工具类别"), categories, copy.allowed_tool_categories || [], value => { copy.allowed_tool_categories = value; }),
    copy => fieldset(tr("生成参数与数据格式"), '', copy, [
      f('temperature', 'Temperature', 'float', {nullable: true}), f('max_tokens', tr("最大输出"), 'int', {nullable: true}),
      f('max_turns', tr("最多轮数"), 'int', {nullable: true}), f('input_schema', tr("输入 Schema"), 'json'), f('output_schema', tr("输出 Schema"), 'json')
    ])
  ], copy => {
    required(copy.id, tr("标识")); required(copy.name, tr("名称"));
    if (index < 0 && list.some(value => value.id === copy.id)) throw Error(tr("能力标识已存在"));
    if (index >= 0 && defaults.some(value => value.id === item.id) && copy.id !== item.id) throw Error(tr("内置能力不能修改标识"));
    if (index < 0) list.push(copy); else list[index] = copy;
    w.draft.app_settings.capabilities.capabilities = list;
  });
  body.append(node('div', {class: 'toolbar'}, node('p', {}, tr("能力可以进行单轮转换或受限 Agent 执行。")), button(tr("＋ 添加能力"), () => edit({
    id: '', name: '', enabled: true, exposure: 'tool', runtime: 'single_turn', model_target: {source: 'auxiliary'},
    description: '', prompt: '', input_schema: {}, output_schema: {}, allowed_tool_categories: [], max_turns: null, temperature: null, max_tokens: null
  }, -1))));
  for (const [index, item] of list.entries()) body.append(node('div', {class: 'setting-row'}, node('div', {}, node('strong', {}, item.name), node('small', {}, item.description)),
    button(item.enabled ? tr("停用") : tr("启用"), () => { item.enabled = !item.enabled; commit(); }),
    button(tr("配置"), () => edit(item, index)),
    defaults.some(value => value.id === item.id) ? null : button(tr("删除"), () => remove(w, item.name, () => { list.splice(index, 1); w.draft.app_settings.capabilities.capabilities = list; }))));
}
