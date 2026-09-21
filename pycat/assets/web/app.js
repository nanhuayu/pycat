import {Api} from './api.js';
import {$, node, button, toast, modal, closeModal, picker, reader, form, markdown} from './ui.js';
import {showConfig, configureSettings, settingsActive, closeSettings} from './config.js';
import {fieldset, choices, categories} from './fields.js';
import {icon, installIcons} from './icons.js';
import {showChannels} from './channels.js';
import {materials, trace, openMaterial, inspector, contextDetails} from './views.js';

const api = new Api();
const state = {session: null, operations: {}, commands: [], runs: new Map(), refs: [], mentions: [], drafts: new Map(), submitting: false};
const compactLayout = matchMedia('(max-width: 900px)');
let selectionVersion = 0, listVersion = 0;
state.presentation = {};
const showThinking = () => state.session?.settings?.show_thinking ?? state.presentation.show_thinking ?? true;
const messageSignature = message => JSON.stringify([message, showThinking()]);
const settingsRefresh = () => reloadSession({configuration: true});
const welcome = $('#welcome');
const guarded = action => async (...args) => { try { return await action(...args); } catch (error) { toast(error.message, true); } };
const currentRun = () => [...state.runs.values()].find(run => !run.done && run.session === state.session?.id);
const scrollBottom = () => { const view = $('#messages'); view.scrollTop = view.scrollHeight; };
const operation = (name, args = {}) => api.operation(name, args);
const saveDraft = () => {
  if (state.session) {
    state.drafts.delete(state.session.id);
    state.drafts.set(state.session.id, {text: $('#composer').value, refs: state.refs, mentions: state.mentions, edit: state.edit});
    if (state.drafts.size > 100) state.drafts.delete(state.drafts.keys().next().value);
  }
};
installIcons();
configureSettings({session: () => state.session, library: () => openLibrary(), sessionSettings, exportSession, perform});
state.inspectorTab = 'tasks';
let inspectorVersion = 0;
const referenceActions = () => ({addReference: ref => { state.refs.push(ref); renderRefs(); }, perform, library: openLibrary, refresh: reloadSession});
async function openLibrary(tab = 'materials') { await ensureSession(); return materials(api, state.session, referenceActions().addReference, perform, tab); }
async function refreshInspector() {
  const version = ++inspectorVersion, session = state.session;
  if ($('#inspector').hidden) return;
  const projection = node('div');
  await inspector(api, session, projection, state.inspectorTab, referenceActions());
  if (version !== inspectorVersion) return;
  $('#inspector-content').replaceChildren(projection);
  $('#state-version').textContent = '当前状态 · v' + (session?.state?.state_version || 0);
}
function showInspector(open) {
  $('#inspector').hidden = !open; $('#toggle-inspector').setAttribute('aria-expanded', String(open));
  if (open) { document.body.classList.remove('sidebar-open'); guarded(refreshInspector)(); }
}
function toggleInspector() {
  const open = $('#inspector').hidden;
  showInspector(open); localStorage.setItem('pycat-inspector', String(open));
}
async function refreshContext() {
  const session = state.session;
  if (!session) { $('#context-value').textContent = '—'; return; }
  const value = await operation('sessions.context', {session: session.id});
  if (state.session?.id === session.id) {
    $('#context-value').textContent = (value.usage_ratio * 100).toFixed(0) + '%';
    $('#context').title = '上下文估算 ' + value.context_tokens.toLocaleString() + ' / ' + value.budget.effective_prompt_limit.toLocaleString() + ' Token';
  }
}

async function refreshSessions() {
  const version = ++listVersion;
  const rows = await operation('sessions.list', {query: $('#session-search').value, limit: 200});
  if (version !== listVersion) return;
  const groups = new Map();
  for (const row of rows) { const key = row.work_dir || ''; if (!groups.has(key)) groups.set(key, []); groups.get(key).push(row); }
  $('#sessions').replaceChildren();
  for (const [path, items] of groups) {
    const heading = node('div', {class: 'project-heading', title: path || '独立会话'}, icon(path ? 'folder' : 'chat'),
      node('span', {}, path ? path.split(/[\\/]/).filter(Boolean).at(-1) : '最近'));
    if (path) heading.append(button('＋', guarded(() => newSession(path))), button('···', () => picker('项目操作', [
      {label: '在此新建会话', action: () => newSession(path)}, {label: '复制完整路径', action: async () => { await navigator.clipboard.writeText(path); toast('已复制路径'); }},
      {label: '查看项目资料', action: async () => { await selectSession(items[0].id); await openLibrary(); }}
    ], guarded(async item => { closeModal(); await item.action(); }))));
    $('#sessions').append(heading);
    for (const item of items) {
      const menu = button('···', guarded(async () => { await selectSession(item.id); await sessionMenu(); }), 'row-menu'); menu.setAttribute('aria-label', (item.title || '未命名会话') + '的操作');
      $('#sessions').append(node('div', {class: 'session-row' + (item.id === state.session?.id ? ' active' : '')},
        button((item.pinned ? '⌁ ' : '') + (item.title || '未命名会话'), guarded(() => selectSession(item.id)), 'session-link'), menu));
    }
  }
  if (!rows.length) $('#sessions').append(node('p', {class: 'empty-small'}, $('#session-search').value ? '没有匹配会话' : '新建会话，开始工作'));
}

function messageElement(message) {
  const tool = message.role === 'tool';
  const article = node(tool ? 'details' : 'article', {class: 'message ' + message.role, 'data-message': message.id});
  article.append(node(tool ? 'summary' : 'div', {class: 'message-heading'},
    !tool && message.role !== 'user' ? node('img', {src: '/brand.svg', alt: ''}) : null,
    tool ? (message.name || '工具结果') : message.role === 'user' ? '你' : 'PyCat',
    message.created_at ? node('time', {datetime: message.created_at}, new Date(message.created_at).toLocaleString([], {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'})) : null));
  if (message.thinking && showThinking()) article.append(node('details', {class: 'thinking'}, node('summary', {}, '思考过程'), markdown(message.thinking)));
  article.append(node('div', {class: 'message-body'}, markdown(message.content)));
  if (message.tool_calls?.length) {
    const steps = node('div');
    for (const call of message.tool_calls) steps.append(node('details', {class: 'tool-card'},
      node('summary', {}, (call.result?.is_error ? '未完成 · ' : '') + (call.function?.name || call.name || '工具调用')),
      node('div', {}, call.result?.content ? markdown(call.result.content) : node('p', {class: 'muted'}, '查看运行检查中的完整调用记录。'),
        node('details', {}, node('summary', {}, '调用参数'), node('pre', {class: 'data-reader'}, typeof call.function?.arguments === 'string' ? call.function.arguments : JSON.stringify(call.function?.arguments || {}, null, 2))))));
    article.append(node('details', {class: 'run-summary'}, node('summary', {}, message.tool_calls.length + ' 次工具调用'), steps),
      button('查看本次运行', guarded(() => trace(api, state.session, perform)), 'text-button'));
  }
  const refs = [...(message.content_refs || []), ...(message.tool_calls || []).flatMap(call => call.result?.is_error ? [] : call.result?.metadata?.content_refs || [])];
  const seen = new Set();
  for (const ref of refs) if (!seen.has(ref.ref)) {
    seen.add(ref.ref);
    article.append(button([icon('file'), ref.name, node('small', {}, ref.mime || ref.kind || '')], guarded(() => openMaterial(api, state.session, ref, referenceActions())), 'artifact-link'));
  }
  if (!tool) {
    const actions = node('div', {class: 'message-actions'}, button('复制', guarded(async () => { await navigator.clipboard.writeText(message.content || ''); toast('已复制'); })));
    if (message.role === 'user') {
      actions.append(button('编辑', () => {
        state.edit = {action: 'edit', message_id: message.id};
        $('#composer').value = message.content; $('#edit-status').hidden = false; $('#composer').focus(); saveDraft();
      }));
      actions.append(button('重试', guarded(() => submit('', {action: 'retry', message_id: message.id}))));
      actions.append(button('删除', () => modal('删除消息', node('p', {}, '删除此轮及之后的消息？'), [button('取消', closeModal), button('删除', guarded(async () => {
        await operation('sessions.remove', {session: state.session.id, message: message.id, expected_revision: state.session.revision}); closeModal(); await reloadSession();
      }), 'danger')])));
    }
    article.append(actions);
  }
  article.dataset.signature = messageSignature(message);
  return article;
}

function renderSession({prepend = false} = {}) {
  const session = state.session, container = $('#messages'), atBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 100;
  $('#session-title').textContent = session?.title || '新会话';
  $('#workspace').textContent = (session?.work_dir || '个人空间') + ' ⌄';
  $('#model').textContent = (session?.model || '选择模型') + ' ⌄';
  $('#model').title = session?.provider_name || '选择模型';
  $('#mode').textContent = (session?.mode || 'chat') + ' ⌄';
  $('#permissions').textContent = ({default: '默认权限', ask: '逐次询问', allow: '允许工具', deny: '禁用工具', custom: '自定义权限'}[session?.settings?.tool_approval] || '默认权限') + ' ⌄';
  const existing = new Map([...container.querySelectorAll('[data-message]')].map(element => [element.dataset.message, element]));
  const nodes = (session?.messages || []).map(message => {
    const old = existing.get(message.id); existing.delete(message.id);
    if (old?.dataset.signature === messageSignature(message)) return old;
    const updated = messageElement(message); old?.replaceWith(updated); return updated;
  });
  existing.forEach(element => element.remove());
  container.querySelector('.load-older')?.remove();
  welcome.hidden = !!nodes.length;
  if (!welcome.parentElement) container.prepend(welcome);
  if (session?.message_offset > 0) container.prepend(button('加载更早的消息', guarded(async () => {
    const height = container.scrollHeight, offset = Math.max(0, session.message_offset - 100);
    const page = await operation('sessions.read', {session: session.id, offset, limit: session.message_offset - offset});
    if (state.session !== session) return;
    state.session = {...session, messages: [...page.messages, ...session.messages], message_offset: offset};
    renderSession({prepend: true}); container.scrollTop += container.scrollHeight - height;
  }), 'load-older'));
  nodes.forEach(element => container.append(element));
  renderRun(); updateMessageNavigation(); guarded(refreshInspector)(); guarded(refreshContext)();
  if (atBottom && !prepend) scrollBottom();
}

async function refreshConfiguration(saved = false) {
  const config = (await operation('config.read')).values.app_settings;
  state.presentation = config;
  if (saved) for (const key of ['pycat-theme', 'pycat-sidebar-collapsed', 'pycat-inspector']) localStorage.removeItem(key);
  const theme = localStorage.getItem('pycat-theme') || config.theme;
  document.documentElement.dataset.theme = theme === 'auto' ? (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light') : theme;
  document.documentElement.dataset.accent = config.accent;
  const inspectorPreference = localStorage.getItem('pycat-inspector');
  const inspectorOpen = !compactLayout.matches && (inspectorPreference === null ? Boolean(config.show_stats) : inspectorPreference === 'true');
  showInspector(inspectorOpen);
  if (saved || localStorage.getItem('pycat-sidebar-collapsed') === null) document.body.classList.toggle('sidebar-collapsed', config.show_sidebar === false);
}
async function reloadSession({configuration = false} = {}) {
  if (configuration) await refreshConfiguration(true);
  const session = state.session, version = selectionVersion;
  const updated = session ? await operation('sessions.read', {session: session.id}) : null;
  if (version !== selectionVersion || session?.id !== state.session?.id) return;
  state.session = updated;
  renderSession(); await refreshSessions();
}

async function selectSession(id) {
  if (settingsActive()) { closeSettings(() => selectSession(id)); return; }
  const version = ++selectionVersion;
  saveDraft();
  const selected = await operation('sessions.read', {session: id});
  if (version !== selectionVersion) return;
  ++completionVersion; $('#completions').hidden = true;
  state.session = selected;
  const draft = state.drafts.get(id) || {text: '', refs: [], mentions: []};
  $('#composer').value = draft.text; state.refs = draft.refs; state.mentions = draft.mentions;
  state.edit = draft.edit || null; $('#edit-status').hidden = !state.edit;
  localStorage.setItem('pycat-session', id);
  renderSession(); renderRefs(); await refreshSessions(); scrollBottom();
  document.body.classList.remove('sidebar-open'); $('#composer').focus();
}

async function newSession(work_dir) {
  if (settingsActive()) { closeSettings(() => newSession(work_dir)); return; }
  const item = await operation('sessions.create', {work_dir: typeof work_dir === 'string' ? work_dir : state.session?.work_dir || ''});
  await selectSession(item.id);
}
async function ensureSession() { if (!state.session) await newSession(); return state.session; }

function renderRefs() {
  const items = [...state.refs.map(ref => ({...ref, kind: 'file'})), ...state.mentions];
  $('#draft-refs').replaceChildren(...items.map(ref => button(`${ref.kind === 'file' ? '▤' : '@'} ${ref.name || ref.label || ref.id} ×`, () => {
    state.refs = state.refs.filter(item => item !== ref && item.ref !== ref.ref);
    state.mentions = state.mentions.filter(item => item !== ref); renderRefs();
  }, 'reference-chip')));
  $('#draft-refs').hidden = !items.length;
}

async function submit(text = $('#composer').value.trim(), revision = null) {
  revision ||= state.edit || null;
  if (state.submitting || !text && !state.refs.length && !revision) return;
  const active = currentRun();
  if (active) {
    if (revision || state.refs.length) throw Error('运行中请发送文字引导；附件和消息修改可在本轮结束后提交。');
    const result = await api.json(`/runs/${active.id}/guidance`, {method: 'POST', body: {text}});
    if (!result.accepted) throw Error('本轮已结束，请重新发送。');
    $('#composer').value = ''; toast('引导已提交'); return;
  }
  state.submitting = true; $('#send').disabled = true;
  try {
    if (!text.startsWith('/')) await ensureSession();
    const request = {text, conversation_id: state.session?.id || null, expected_revision: state.session?.revision || null,
      references: revision ? [] : state.refs.map(item => item.ref),
      mentions: revision ? [] : state.mentions.filter(item => text.includes(item.insert_text.trim())).map(({kind, id}) => ({kind, id})), revision};
    const fingerprint = JSON.stringify(request);
    if (state.pendingSubmit?.fingerprint !== fingerprint) state.pendingSubmit = {fingerprint, request_id: crypto.randomUUID()};
    const result = await api.input({...request, request_id: state.pendingSubmit.request_id});
    state.pendingSubmit = null;
    if (!revision || state.edit) { $('#composer').value = ''; state.refs = []; state.mentions = []; state.edit = null; $('#edit-status').hidden = true; renderRefs(); saveDraft(); }
    $('#completions').hidden = true;
    if (result.run_id) {
      const run = {id: result.run_id, session: result.session || state.session?.id, text: '', pending: new Map(), done: false, draft: text};
      state.runs.set(run.id, run); renderRun(); observe(run);
    } else {
      if (result.session) await selectSession(result.session);
      if (result.message) reader('提示', result.message);
      if (result.panel) await openPanel(result.panel, result.data);
      if (result.kind === 'exit') toast('当前运行已保留，可以关闭此页面。');
    }
  } catch (error) {
    if (error.status && error.status < 500) state.pendingSubmit = null;
    throw error;
  } finally { state.submitting = false; $('#send').disabled = false; }
}

function renderRun() {
  const view = $('#messages'), atBottom = view.scrollHeight - view.scrollTop - view.clientHeight < 100;
  const run = currentRun(); $('#run-status').hidden = !run; $('#cancel').disabled = !!run?.cancelling;
  $('#pending').hidden = !run?.pending.size;
  $('#run-label').textContent = run?.pending.size ? '等待你的回复' : run?.cancelling ? '正在停止…' : run?.label || '正在处理';
  $('#send').title = run ? '发送引导' : '发送消息';
  $('#composer').placeholder = run ? '补充说明，指导当前任务…' : '描述任务… 输入 / 查看命令，@ 添加引用';
  let element = $('#live-message');
  if (!run) { element?.remove(); return; }
  welcome.hidden = true;
  if (!element) { element = node('article', {id: 'live-message', class: 'message assistant streaming'}, node('div', {class: 'message-role'}, 'PyCat'), node('div', {class: 'stream-content'})); $('#messages').append(element); }
  element.querySelector('.stream-content').textContent = run.text;
  if (atBottom) scrollBottom();
}

async function observe(run) {
  try {
    for await (const event of api.events(run.id)) {
      if (event.conversation_id) run.session = event.conversation_id;
      if (event.type === 'event') {
        if (event.kind === 'text_delta') run.text = (run.text + String(event.data || '')).slice(-32768);
        else if (event.tool_name) run.label = '正在使用 ' + event.tool_name;
        if (!run.timer) run.timer = setTimeout(() => { run.timer = null; renderRun(); }, 50);
      } else if (event.type === 'interaction') {
        run.pending.set(event.id, event); renderRun(); if (run === currentRun()) showInteraction(run, event);
      } else if (event.type === 'interaction_closed') {
        run.pending.delete(event.id); if (state.interaction === event.id) { closeModal(); state.interaction = null; } renderRun();
      } else if (event.type === 'reset') {
        run.session = event.session; run.text = ''; run.pending = new Map(event.pending.map(item => [item.id, item]));
        if (run.session === state.session?.id) await reloadSession();
        if (event.done) await finishRun(run, event.final);
      } else if (event.type === 'final') await finishRun(run, event);
      else if (event.type === 'operation_result') reader('操作结果', event.data);
    }
  } catch (error) { run.label = '连接中断，可刷新页面恢复'; toast(error.message, true); renderRun(); }
}

async function finishRun(run, result) {
  run.done = true; clearTimeout(run.timer);
  if (run.pending.has(state.interaction)) { closeModal(); state.interaction = null; }
  run.pending.clear();
  if (result.error) toast(result.error, true);
  if (result.data !== null && result.data !== undefined) reader('操作结果', result.data);
  if (run.session === state.session?.id) {
    await reloadSession();
    if (result.status === 'failed' && !state.session.messages.some(message => message.role === 'user' && message.content === run.draft) && !$('#composer').value) $('#composer').value = run.draft;
    scrollBottom();
  } else await refreshSessions();
  state.runs.delete(run.id); renderRun();
}

function showInteraction(run, item) {
  if (settingsActive() || $('#dialog').open) {
    toast('本轮正在等待回复，可返回会话处理。');
    return;
  }
  state.interaction = item.id;
  const reply = guarded(async decision => { await api.json(`/runs/${run.id}/interactions/${item.id}`, {method: 'POST', body: decision}); closeModal(); state.interaction = null; });
  if (item.kind === 'approval') {
    modal('批准工具操作', [node('p', {}, item.payload.reason || '此操作需要你的批准。'), node('pre', {class: 'data-reader'}, JSON.stringify(item.payload, null, 2))],
      [button('拒绝', () => reply({approved: false})), button('允许本次', () => reply({approved: true, read_scope: 'call'}), 'primary'), ...(item.payload.external_path ? [button('允许本轮读取', () => reply({approved: true, read_scope: 'run'}))] : [])], '等待回复');
  } else {
    const options = (item.payload.options || []).map(option => {
      const input = node('input', {type: item.payload.multiple ? 'checkbox' : 'radio', name: 'answer', value: option.label});
      return {input, element: node('label', {class: 'answer-option'}, input, node('span', {}, option.label, node('small', {}, option.description || '')))};
    });
    const free = node('textarea', {rows: 3, placeholder: '补充说明（可选）', 'aria-label': '补充说明'});
    modal(item.payload.text || '需要你的选择', [node('div', {class: 'answer-options'}, options.map(item => item.element)), free],
      [button('跳过', () => reply({selected: [], freeText: null, skipped: true})), button('提交', () => {
        const selected = options.filter(item => item.input.checked).map(item => item.input.value);
        if (!selected.length && !free.value.trim()) { toast('请选择选项或填写补充说明'); return; }
        reply({selected, freeText: free.value.trim() || null, skipped: false});
      }, 'primary')], '等待回复');
  }
}

async function selectModel() {
  await ensureSession(); const models = await operation('model.list');
  if (!models.length) { await showConfig(api, settingsRefresh); return; }
  picker('选择模型', models.map(model => ({label: model.model, detail: model.provider, ...model})), guarded(async item => {
    await operation('sessions.select', {session: state.session.id, model: item.ref, expected_revision: state.session.revision}); closeModal(); await reloadSession();
  }));
}
async function selectMode() {
  await ensureSession(); const modes = await operation('mode.list', {work_dir: state.session.work_dir});
  picker('选择模式', modes.filter(mode => ['primary', 'both'].includes(mode.profile_kind) && mode.slug !== 'channel').map(mode => ({label: mode.name, detail: mode.purpose, ...mode})), guarded(async item => {
    await operation('sessions.select', {session: state.session.id, mode: item.slug, expected_revision: state.session.revision}); closeModal(); await reloadSession();
  }));
}
async function permissions() {
  await ensureSession();
  form('工具与文件权限', [
    {name: 'tool_approval', label: '工具批准', options: [{value: 'default', label: '默认'}, {value: 'ask', label: '逐次询问'}, {value: 'allow', label: '允许'}, {value: 'deny', label: '禁用工具'}, {value: 'custom', label: '自定义规则'}]},
    {name: 'filesystem_mode', label: '文件范围', options: [{value: 'confined', label: '当前工作区'}, {value: 'full_access', label: '完整文件访问'}]}
  ], {tool_approval: state.session.settings?.tool_approval || 'default', filesystem_mode: state.session.settings?.filesystem_mode || 'confined'}, async values => {
    await operation('sessions.permissions', {session: state.session.id, ...values, expected_revision: state.session.revision}); closeModal(); await reloadSession();
  }, {description: '变更即时作用于后续工具调用。正在执行的调用不会被重新执行。'});
}

async function openPanel(panel, data) {
  if (panel === 'model') return selectModel();
  if (panel === 'mode') return selectMode();
  if (panel === 'config') return showConfig(api, settingsRefresh);
  if (panel === 'channels') return showChannels(api, settingsRefresh);
  if (panel === 'permissions') return permissions();
  if (panel === 'resume') return resumePicker();
  if (panel === 'rename') return rename();
  if (panel === 'mcp') return showConfig(api, settingsRefresh, 'mcp');
  if (panel === 'context') { await ensureSession(); return contextDetails(api, state.session, perform); }
  if (panel === 'export') return exportSession(data || 'markdown');
  if (panel === 'copy') { await navigator.clipboard.writeText(state.session?.messages.filter(item => item.role === 'assistant').at(-1)?.content || ''); toast('已复制'); return; }
  const name = {agents: 'mode.list', status: 'doctor', context: 'sessions.read', mcp: 'mcp.list', channels: 'channels.list', doctor: 'doctor'}[panel];
  if (name) return perform(name, {session: state.session?.id, work_dir: state.session?.work_dir || ''});
  return resources(panel);
}

async function sessionSettings() {
  await ensureSession();
  const value = await operation('sessions.settings', {session: state.session.id});
  const settings = {session_instructions: '', memory_enabled: true, show_thinking: true, max_context_messages: null,
    pycat_assistant_enabled: true, tool_selection: {}, channel_notice_policy: 'notice',
    allowed_channel_sources: null, trusted_channel_sources: [], ...value.settings};
  const llm = Object.fromEntries(Object.entries(value.llm).filter(([key]) => ['temperature', 'top_p', 'max_tokens', 'stream', 'reasoning_mode'].includes(key)));
  const content = node('div', {}, fieldset('会话', '仅应用于当前会话。', settings, [
    {key: 'session_instructions', label: '会话指令', type: 'text'}, {key: 'memory_enabled', label: '启用记忆', type: 'bool'},
    {key: 'show_thinking', label: '显示思考过程', type: 'bool'}, {key: 'max_context_messages', label: '最多上下文消息', type: 'int', nullable: true, min: 1},
    {key: 'pycat_assistant_enabled', label: '使用 PyCat 助手提示', type: 'bool', help: 'Agent 模式始终启用；Chat 模式可以关闭。'}
  ]), fieldset('生成参数', '留空表示继承模型档案的默认值。', llm, [
    {key: 'temperature', label: 'Temperature', type: 'float', nullable: true}, {key: 'top_p', label: 'Top P', type: 'float', nullable: true},
    {key: 'max_tokens', label: '最大输出', type: 'int', nullable: true, min: 1}, {key: 'stream', label: '流式响应', type: 'bool'},
    {key: 'reasoning_mode', label: '思考强度', options: ['inherit', 'off', 'on', 'auto', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra']}
  ]));
  const restrictions = node('details', {}, node('summary', {}, '工具与频道'),
    choices('允许的工具类别', categories, settings.tool_selection?.allowed_categories || categories.map(item => item[0]),
      values => { settings.tool_selection = {...settings.tool_selection, allowed_categories: values}; }),
    fieldset('频道来源', '来源名称与频道配置一致；留空允许来源表示继承默认值。每行一项。', settings, [
      {key: 'allowed_channel_sources', label: '允许来源', type: 'lines', nullable: true},
      {key: 'trusted_channel_sources', label: '信任来源', type: 'lines'},
      {key: 'channel_notice_policy', label: '来源提示', options: [{value: 'notice', label: '默认提醒'}, {value: 'strict', label: '严格限制未信任来源'}, {value: 'silent', label: '简洁提示'}]}
    ]));
  content.append(restrictions);
  const error = node('p', {class: 'form-error', role: 'alert'}); content.append(error);
  const save = button('保存', async () => {
    const invalid = content.querySelector(':invalid'); if (invalid) { invalid.reportValidity(); return; }
    save.disabled = true;
    try {
      const patch = Object.fromEntries(['session_instructions', 'memory_enabled', 'show_thinking', 'max_context_messages',
        'pycat_assistant_enabled', 'tool_selection', 'allowed_channel_sources', 'trusted_channel_sources', 'channel_notice_policy'].map(key => [key, settings[key]]));
      await operation('sessions.settings', {session: state.session.id, settings: patch, llm, expected_revision: value.revision});
      closeModal(); await reloadSession();
    } catch (failure) { error.textContent = failure.message; } finally { save.disabled = false; }
  }, 'primary');
  modal('会话设置', content, [button('取消', closeModal), save]);
}

async function perform(name, initial = {}) {
  const descriptor = state.operations[name]; if (!descriptor) throw Error('操作不可用：' + name);
  const values = {session: state.session?.id, work_dir: state.session?.work_dir || '', expected_revision: state.session?.revision, ...initial};
  const fields = descriptor.parameters.filter(field => !(field.name in values && values[field.name] !== undefined && ['session', 'work_dir', 'expected_revision'].includes(field.name)));
  const run = async data => {
    const arguments_ = Object.fromEntries(descriptor.parameters.map(field => [field.name, field.name in data ? data[field.name] : values[field.name] ?? field.default]));
    const result = await operation(name, arguments_);
    if (result?.run_id) { const observed = {id: result.run_id, session: result.session, text: '', pending: new Map(), done: false}; state.runs.set(observed.id, observed); closeModal(); observe(observed); renderRun(); }
    else reader(descriptor.label, result);
    if (name.startsWith('sessions.')) await reloadSession();
  };
  if (!fields.length) return run({});
  form(descriptor.label, fields.map(field => ({...field, type: /^(dict|list|bool)/.exec(field.type)?.[1] || field.type, label: field.name})), values, run, {label: '执行'});
}

function resources(group = '') {
  if (!group) {
    const commands = [
      {label: '新建会话', detail: '/new · Ctrl N', action: newSession},
      {label: '继续会话', detail: '/resume', action: resumePicker},
      {label: '选择模型', detail: '/model', action: () => openPanel('model')},
      {label: '选择模式', detail: '/mode', action: () => openPanel('mode')},
      {label: '工具与文件权限', detail: '/permissions', action: () => openPanel('permissions')},
      {label: '重命名会话', detail: '/rename', action: rename},
      {label: '会话设置', detail: '指令、生成参数、工具与频道', action: sessionSettings},
      {label: '资料与记忆', detail: '阅读、下载、项目知识', action: openLibrary},
      {label: '运行检查', detail: '过程与当前状态 · Ctrl J', action: async () => { await ensureSession(); await trace(api, state.session, perform); }},
      {label: '设置', detail: '/config · 模型、工具与能力、消息通道', action: () => showConfig(api, settingsRefresh)},
      {label: '导入会话', detail: 'PyCat JSON', action: () => $('#import-input').click()},
      {label: '导出会话', detail: '/export', action: exportSession},
      {label: '所有管理操作', detail: '搜索工具、进程、SSH 等高级操作', action: () => resources('*')}
    ];
    picker('命令', commands, guarded(async item => { closeModal(); await item.action(); })); return;
  }
  if (['skills', 'channels', 'mcp', 'providers'].includes(group)) return showConfig(api, settingsRefresh, group);
  if (['memory', 'materials'].includes(group)) return openLibrary(group);
  const items = Object.entries(state.operations).filter(([name]) => !name.startsWith('input.') && !name.startsWith('config.') && (group === '*' || name.startsWith(group + '.')));
  picker(group === '*' ? '所有管理操作' : '管理 ' + group, items.map(([name, descriptor]) => ({label: descriptor.label, detail: name, name})), guarded(item => perform(item.name)));
}
async function resumePicker() {
  const rows = await operation('sessions.list', {limit: 200});
  picker('继续会话', rows.map(row => ({label: row.title || '未命名会话', detail: row.work_dir || '个人空间', id: row.id})), guarded(async item => { closeModal(); await selectSession(item.id); }));
}
async function rename() {
  await ensureSession(); form('重命名会话', [{name: 'title', label: '名称', required: true}], {title: state.session.title}, async ({title}) => {
    await operation('sessions.rename', {session: state.session.id, title}); closeModal(); await reloadSession();
  });
}
async function exportSession(format = 'markdown') {
  await ensureSession(); const extensions = {markdown: 'md', json: 'json', html: 'html', docx: 'docx'};
  if (typeof format !== 'string' || !extensions[format]) format = 'markdown';
  await api.download(state.session.id, 'export?format=' + format, (state.session.title || 'session') + '.' + extensions[format]);
}
async function sessionMenu() {
  await ensureSession();
  picker('会话操作', [
    {label: '重命名', action: rename}, {label: state.session.pinned ? '取消置顶' : '置顶', action: async () => { await operation('sessions.pin', {session: state.session.id, pinned: !state.session.pinned}); closeModal(); await reloadSession(); }},
    {label: '会话设置', action: sessionSettings},
    {label: '导出', action: () => picker('导出格式', ['markdown', 'json', 'html', 'docx'].map(value => ({label: value})), guarded(async item => { await exportSession(item.label); closeModal(); }))},
    {label: '导入', action: () => { closeModal(); $('#import-input').click(); }},
    {label: '压缩上下文', action: () => perform('sessions.compact')},
    {label: '归档', action: async () => { await operation('sessions.archive', {session: state.session.id}); closeModal(); state.session = null; await refreshSessions(); renderSession(); }},
    {label: '删除', action: () => modal('删除会话', node('p', {}, '会话及其消息将被删除。此操作无法撤销。'), [button('取消', closeModal), button('删除', guarded(async () => { await operation('sessions.delete', {session: state.session.id}); closeModal(); state.session = null; renderSession(); await refreshSessions(); }), 'danger')])}
  ], guarded(item => item.action()));
}

let completionVersion = 0, completionTimer;
async function complete() {
  const text = $('#composer').value, cursor = $('#composer').selectionStart, version = ++completionVersion;
  const session = state.session?.id || null;
  const result = await operation('input.complete', {text, cursor, utf16: true, session});
  if (version !== completionVersion || session !== (state.session?.id || null) || text !== $('#composer').value || cursor !== $('#composer').selectionStart) return;
  const popup = $('#completions'); popup.hidden = !result.candidates.length;
  popup.replaceChildren(...result.candidates.map(item => button([node('span', {}, item.label), node('small', {}, {file: '文件', agent: 'Agent', channel: '频道', run: '运行', content: '资料', command: '命令'}[item.kind] || item.kind)], guarded(async () => {
    if (session !== (state.session?.id || null) || text !== $('#composer').value || cursor !== $('#composer').selectionStart) { popup.hidden = true; return; }
    const start = Array.from(text).slice(0, result.query.start_pos).join('').length, end = Array.from(text).slice(0, result.query.end_pos).join('').length;
    let insert = item.insert_text || '';
    if (item.kind === 'file' && item.terminal) { state.refs.push({ref: 'workspace:' + item.value, name: item.label}); insert = ''; }
    else if (item.terminal && item.kind !== 'command') {
      state.mentions = state.mentions.filter(other => other.kind !== item.kind || other.id !== item.value);
      state.mentions.push({kind: item.kind, id: item.value, label: item.label, insert_text: insert});
    }
    $('#composer').setRangeText(insert, start, end, 'end'); $('#composer').focus(); popup.hidden = true; renderRefs();
    if (!item.terminal) await complete();
  }), 'completion-row')));
}

async function upload(files) {
  const session = await ensureSession(), refs = [];
  for (const file of files) { const result = await api.upload(session.id, file); refs.push(...result.refs); }
  if (state.session?.id === session.id) { state.refs.push(...refs); renderRefs(); }
  else {
    const draft = state.drafts.get(session.id) || {text: '', refs: [], mentions: []};
    draft.refs.push(...refs); state.drafts.set(session.id, draft);
    toast('附件已保留在原会话草稿');
  }
  $('#file-input').value = '';
}

async function boot() {
  if (!api.token) {
    form('连接工作台', [{name: 'token', label: '访问令牌', secret: true, required: true}], {}, async ({token}) => {
      api.token = token; sessionStorage.setItem('pycat-token', token); await boot(); closeModal();
    }, {label: '连接', description: '使用 pycat serve 输出的访问链接或令牌。'}); return;
  }
  let bootstrap;
  try { bootstrap = await api.json('/bootstrap'); }
  catch (error) { if (error.status === 401) { api.token = ''; sessionStorage.removeItem('pycat-token'); await boot(); return; } throw error; }
  state.operations = bootstrap.operations; state.commands = bootstrap.commands;
  await refreshConfiguration();
  $('#connection-label').textContent = '已连接 · ' + bootstrap.version;
  for (const item of bootstrap.runs.filter(run => !run.done)) {
    const run = {id: item.run_id, session: item.session, pending: new Map(item.pending.map(value => [value.id, value])), text: '', done: false};
    state.runs.set(run.id, run); observe(run);
  }
  const saved = localStorage.getItem('pycat-session');
  if (saved) { try { await selectSession(saved); } catch { localStorage.removeItem('pycat-session'); await refreshSessions(); } }
  else await refreshSessions();
  renderSession();
}

$('#new').onclick = guarded(newSession);
$('#send').onclick = guarded(() => submit());
$('#model').onclick = guarded(selectModel); $('#mode').onclick = guarded(selectMode); $('#permissions').onclick = guarded(permissions);
$('#settings').onclick = guarded(() => showConfig(api, settingsRefresh, 'general'));
$('#about').onclick = guarded(() => showConfig(api, settingsRefresh, 'about'));
$('#commands').onclick = () => resources();
$('#session-menu').onclick = guarded(sessionMenu); $('#resume-all').onclick = guarded(resumePicker);
$('#trace').onclick = guarded(async () => { await ensureSession(); await trace(api, state.session, perform); });
$('#materials').onclick = () => closeSettings(guarded(() => openLibrary()));
$('#context').onclick = guarded(async () => { await ensureSession(); await contextDetails(api, state.session, perform); });
$('#toggle-inspector').onclick = toggleInspector;
$('#state-version').onclick = guarded(async () => { await ensureSession(); await trace(api, state.session, perform, 'state'); });
$('#cancel-edit').onclick = () => { state.edit = null; $('#edit-status').hidden = true; $('#composer').value = ''; saveDraft(); };
$('#project-new').onclick = () => form('添加项目', [{name: 'work_dir', label: '项目目录或 SSH 工作区', required: true}], {}, async ({work_dir}) => { await newSession(work_dir); closeModal(); });
for (const item of document.querySelectorAll('[data-inspector]')) item.onclick = () => {
  state.inspectorTab = item.dataset.inspector; document.querySelectorAll('[data-inspector]').forEach(value => value.classList.toggle('active', value === item)); guarded(refreshInspector)();
};
$('#workspace').onclick = guarded(async () => {
  await ensureSession(); form('工作区', [{name: 'work_dir', label: '本地目录或 SSH 工作区', required: false, default: ''}], {work_dir: state.session.work_dir}, async values => {
    const result = await operation('workspace.select', {session: state.session.id, ...values});
    if (result.ok === false || result.error) throw Error(result.error || '无法切换工作区'); closeModal(); await reloadSession();
  });
});
$('#attach').onclick = () => $('#file-input').click();
$('#file-input').onchange = guarded(event => upload(event.target.files));
$('#import-input').onchange = guarded(async event => {
  const file = event.target.files[0]; if (!file) return;
  const result = await api.json('/import?name=' + encodeURIComponent(file.name), {method: 'POST', body: file, raw: true});
  await selectSession(result.id); event.target.value = '';
});
$('#mention').onclick = guarded(async () => { const input = $('#composer'); input.setRangeText((input.selectionStart && !/\s/.test(input.value[input.selectionStart - 1]) ? ' ' : '') + '@', input.selectionStart, input.selectionEnd, 'end'); input.focus(); await complete(); });
$('#composer').oninput = () => { clearTimeout(completionTimer); completionTimer = setTimeout(guarded(complete), 160); saveDraft(); };
$('#composer').onkeydown = event => {
  if (event.isComposing) return;
  if (event.key === 'ArrowDown' && !$('#completions').hidden) { event.preventDefault(); $('#completions button')?.focus(); }
  if (event.key === 'Escape') { ++completionVersion; $('#completions').hidden = true; }
  if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); guarded(() => submit())(); }
};
$('#completions').onkeydown = event => {
  if (event.key === 'Escape') { $('#completions').hidden = true; $('#composer').focus(); }
  if (['ArrowDown', 'ArrowUp'].includes(event.key)) { event.preventDefault(); const next = event.key === 'ArrowDown' ? event.target.nextElementSibling : event.target.previousElementSibling; (next || $('#composer')).focus(); }
};
$('#cancel').onclick = guarded(async () => { const run = currentRun(); if (run) { await api.json(`/runs/${run.id}/cancel`, {method: 'POST'}); run.cancelling = true; renderRun(); } });
$('#pending').onclick = () => { const run = currentRun(), item = run?.pending.values().next().value; if (item) showInteraction(run, item); };
$('#close-dialog').onclick = closeModal;
$('#toggle-sidebar').onclick = () => {
  if (innerWidth <= 760) { showInspector(false); document.body.classList.remove('sidebar-collapsed'); document.body.classList.toggle('sidebar-open'); }
  else { document.body.classList.toggle('sidebar-collapsed'); localStorage.setItem('pycat-sidebar-collapsed', document.body.classList.contains('sidebar-collapsed')); }
};
compactLayout.addEventListener('change', event => {
  const preference = localStorage.getItem('pycat-inspector');
  showInspector(!event.matches && (preference === null ? Boolean(state.presentation.show_stats) : preference === 'true'));
});
if (localStorage.getItem('pycat-sidebar-collapsed') === 'true') document.body.classList.add('sidebar-collapsed');
$('#theme').onclick = () => { const dark = document.documentElement.dataset.theme !== 'dark'; document.documentElement.dataset.theme = dark ? 'dark' : 'light'; localStorage.setItem('pycat-theme', dark ? 'dark' : 'light'); };
document.documentElement.dataset.theme = localStorage.getItem('pycat-theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
let searchTimer; $('#session-search').oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(guarded(refreshSessions), 160); };
document.querySelectorAll('[data-prompt]').forEach(item => { item.onclick = () => { $('#composer').value = item.dataset.prompt; $('#composer').focus(); }; });
document.addEventListener('keydown', event => {
  if (event.isComposing || $('#dialog').open || settingsActive()) return;
  if (event.ctrlKey || event.metaKey) {
    if (event.key.toLowerCase() === 'k') { event.preventDefault(); resources(); }
    if (event.key.toLowerCase() === 'n') { event.preventDefault(); guarded(newSession)(); }
    if (event.key.toLowerCase() === 'j') { event.preventDefault(); $('#trace').click(); }
    if (event.key.toLowerCase() === 'i' && event.shiftKey) { event.preventDefault(); toggleInspector(); }
  }
  if (event.altKey && ['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) {
    event.preventDefault(); navigateMessage({ArrowUp: -1, ArrowDown: 1, Home: 'first', End: 'last'}[event.key]);
  }
  if (event.key === 'Escape') {
    if ($('#run-dialog')?.open) $('#run-dialog').close();
    else if (currentRun()) $('#cancel').click();
  }
});
document.addEventListener('paste', event => { if (!settingsActive() && !$('#dialog').open && event.clipboardData.files.length) { event.preventDefault(); guarded(() => upload(event.clipboardData.files))(); } });
function updateMessageNavigation() {
  const rows = [...$('#messages').querySelectorAll('article[data-message]')];
  $('#message-navigation').hidden = rows.length < 2;
  const top = $('#messages').getBoundingClientRect().top;
  const index = Math.max(0, rows.findIndex(row => row.getBoundingClientRect().bottom > top + 30));
  $('#previous-message').disabled = index <= 0;
  $('#next-message').disabled = index >= rows.length - 1;
  return {rows, index};
}
function navigateMessage(direction) {
  const {rows, index} = updateMessageNavigation();
  const next = direction === 'first' ? 0 : direction === 'last' ? rows.length - 1 : Math.max(0, Math.min(rows.length - 1, index + direction));
  rows[next]?.scrollIntoView({block: 'start', behavior: 'smooth'});
}
$('#previous-message').onclick = () => navigateMessage(-1); $('#next-message').onclick = () => navigateMessage(1);
$('#messages').addEventListener('scroll', updateMessageNavigation, {passive: true});
guarded(boot)();
