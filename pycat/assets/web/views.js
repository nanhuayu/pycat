import {node, button, modal, reader, form, closeModal, toast, markdown} from './ui.js';
const attempt = action => async () => { try { await action(); } catch (error) { toast(error.message, true); } };
const empty = text => node('p', {class: 'empty-small'}, text);
const sizes = bytes => bytes >= 1048576 ? (bytes / 1048576).toFixed(1) + ' MB' : bytes >= 1024 ? Math.ceil(bytes / 1024) + ' KB' : bytes + ' B';
const labels = {artifact: '成果', wiki: '项目知识', input: '输入附件', file: '文件', workspace: '项目文件', archive: '原文'};

export async function openMaterial(api, session, item, actions = {}) {
  const ref = item.ref?.ref ? item.ref : item;
  if (!ref.ref) { reader(item.title || '资料', item); return; }
  const value = await api.operation('materials.read', {session: session.id, ref: ref.ref});
  const body = node('div', {class: 'material-reader'});
  const content = node('div', {class: 'reader-content'}), toolbar = node('div', {class: 'reader-toolbar'});
  let raw = false, url;
  const render = async () => {
    content.replaceChildren();
    if (/^image\//.test(value.mime)) {
      const response = await api.request('/sessions/' + session.id + '/content?ref=' + encodeURIComponent(ref.ref));
      if (url) URL.revokeObjectURL(url);
      url = URL.createObjectURL(await response.blob()); content.append(node('img', {src: url, alt: value.name, class: 'content-image'}));
    } else if (/^(text\/|application\/(json|xml))/.test(value.mime)) content.append(raw ? node('pre', {class: 'data-reader'}, value.text) : markdown(value.text));
    else content.append(empty(value.mime + ' · ' + sizes(value.size) + '，下载后使用相应应用打开。'));
    if (value.has_more) content.append(node('p', {class: 'muted'}, '预览已截断。下载可获得完整内容。'));
  };
  toolbar.append(button('阅读 / 源码', attempt(async () => { raw = !raw; await render(); })),
    button('复制', attempt(async () => { await navigator.clipboard.writeText(value.text); toast('已复制'); })),
    button('下载', attempt(() => api.download(session.id, 'content?ref=' + encodeURIComponent(ref.ref), value.name))),
    button('来源', () => reader('来源信息', {名称: value.name, 类型: labels[ref.kind] || ref.kind, 范围: ref.workspace || '当前会话', 引用: ref.ref, 版本: ref.digest || '', 来源: ref.source || ''})));
  if (actions.addReference) toolbar.append(button('添加到输入', () => { actions.addReference(ref); closeModal(); }));
  if (ref.kind === 'artifact' && actions.perform) toolbar.append(button('整理为知识', attempt(() => actions.perform('materials.promote', {session: session.id, ref}))));
  if ((/^image\//.test(value.mime) || value.mime === 'application/pdf') && actions.perform) toolbar.append(button('识别文字', attempt(() => actions.perform('materials.ocr', {session: session.id, ref: ref.ref}))));
  if (ref.kind === 'wiki') toolbar.append(button('编辑知识', attempt(async () => {
    const page = await api.operation('knowledge.read', {work_dir: session.work_dir, page: ref.id});
    form('编辑项目知识', [{name: 'title', label: '标题', required: true}, {name: 'summary', label: '摘要', type: 'text', required: true}, {name: 'body', label: '正文', type: 'text', required: true}], page, async values => {
      await api.operation('knowledge.save', {work_dir: session.work_dir, page: {...page, ...values, expected_digest: page.digest}});
      toast('项目知识已保存'); closeModal(); actions.refresh?.();
    });
  })), button('删除知识', attempt(async () => {
    const page = await api.operation('knowledge.read', {work_dir: session.work_dir, page: ref.id});
    modal('删除项目知识', node('p', {}, '删除“' + page.title + '”？来源原文会保留。'), [button('取消', closeModal), button('删除', attempt(async () => {
      await api.operation('knowledge.delete', {work_dir: session.work_dir, page}); closeModal(); actions.refresh?.();
    }), 'danger')]);
  })));
  body.append(node('div', {class: 'reader-meta'}, labels[ref.kind] || '资料', node('span', {}, sizes(value.size)), node('span', {}, ref.kind === 'wiki' ? '项目共享' : '当前会话')), toolbar, content);
  modal(value.name || item.title, body, [actions.back ? button('返回资料', actions.back) : button('关闭', closeModal)]);
  document.querySelector('#dialog').classList.add('wide-dialog');
  document.querySelector('#dialog').addEventListener('close', () => { if (url) URL.revokeObjectURL(url); document.querySelector('#dialog').classList.remove('wide-dialog'); }, {once: true});
  await render();
}

async function memoryPanel(api, session, host, {refresh, compact = false} = {}) {
  const value = await api.operation('memory.read', {work_dir: session.work_dir});
  host.append(node('div', {class: 'toolbar'}, node('p', {}, value.status), button('刷新', refresh),
    value.evolution.failure_count ? button('重试整理', attempt(async () => { await api.operation('memory.retry', {work_dir: session.work_dir}); refresh(); })) : null));
  if (value.unassigned_count) host.append(button('分配 ' + value.unassigned_count + ' 项待归属记忆', attempt(async () => {
    await api.operation('memory.assign', {session: session.id}); refresh();
  })));
  for (const target of value.targets) {
    const scope = target.target === 'user' ? '用户偏好' : '项目记忆';
    const section = node('section', {class: 'memory-group'}, node('div', {class: 'toolbar'}, node('h3', {}, scope),
      node('small', {}, target.used_chars + ' / ' + target.char_limit + ' 字符'), button('＋', () => edit())));
    async function edit(record = null) {
      form(record ? '编辑' + scope : '添加' + scope, [{name: 'text', label: '记忆内容', type: 'text', required: true}], {text: record?.text || ''}, async ({text}) => {
        await api.operation('memory.edit', {session: session.id, target: target.target, expected_digest: target.digest, text, entry_id: record?.id || ''});
        closeModal(); await refresh();
      });
    }
    for (const record of target.records) {
      const row = node('div', {class: 'memory-row'}, node('p', {}, record.text));
      row.append(node('div', {class: 'row-actions'}, button('编辑', () => edit(record)), button('忘记', () => modal('忘记这条记忆？', node('p', {}, record.text),
        [button('取消', closeModal), button('忘记', attempt(async () => {
          await api.operation('memory.edit', {session: session.id, target: target.target, expected_digest: target.digest, entry_id: record.id, forget: true});
          closeModal(); await refresh();
        }), 'danger')])),
        record.sources?.length ? button('来源', () => modal('记忆来源', node('div', {}, record.sources.map(ref => button(ref.name || ref.ref,
          attempt(async () => reader(ref.name || '来源片段', await api.operation('memory.source', {ref}))), 'picker-row'))), [button('关闭', closeModal)])) : null));
      section.append(row);
    }
    if (!target.records.length) section.append(empty('暂无' + scope));
    host.append(section);
  }
  if (!compact && value.evolution.failure_count) host.append(node('details', {}, node('summary', {}, '整理待处理详情'),
    node('pre', {class: 'data-reader'}, JSON.stringify(value.evolution.failure_groups || value.evolution, null, 2))));
}

export async function materials(api, session, addReference, perform, tab = 'materials') {
  let offset = 0, generation = 0;
  const body = node('div'), tabs = node('div', {class: 'settings-tabs'},
    button('资料', attempt(() => materials(api, session, addReference, perform, 'materials')), tab === 'materials' ? 'active' : ''),
    button('记忆', attempt(() => materials(api, session, addReference, perform, 'memory')), tab === 'memory' ? 'active' : ''));
  const refresh = () => materials(api, session, addReference, perform, tab);
  modal('资料与记忆', [tabs, body], [button('关闭', closeModal)], session.work_dir || '个人空间');
  document.querySelector('#dialog').classList.add('wide-dialog');
  if (tab === 'memory') { await memoryPanel(api, session, body, {refresh}); return; }
  const query = node('input', {placeholder: '搜索成果、知识与附件', 'aria-label': '搜索资料'});
  const kind = node('select', {'aria-label': '资料类型'}, [['all', '全部'], ['artifact', '成果'], ['wiki', '项目知识'], ['file', '文件与附件']].map(([value, label]) => node('option', {value}, label)));
  const rows = node('div', {class: 'material-list'}), status = node('small', {class: 'muted'});
  const next = button('下一页', attempt(async () => { offset += 50; await load(); })), previous = button('上一页', attempt(async () => { offset = Math.max(0, offset - 50); await load(); }));
  async function load() {
    const version = ++generation;
    const page = await api.operation('materials.list', {session: session.id, query: query.value, kind: kind.value, offset, limit: 50});
    if (version !== generation) return;
    rows.replaceChildren(...page.items.map(item => button([
      node('span', {}, '▤ ' + item.title), node('small', {}, (labels[item.kind] || item.kind) + ' · ' + (item.summary || item.scope || ''))
    ], attempt(() => openMaterial(api, session, item, {addReference, perform, back: refresh, refresh})), 'material-row')));
    status.textContent = page.total + ' 项 · ' + (page.total ? offset + 1 : 0) + '–' + (offset + page.items.length);
    next.disabled = offset + page.items.length >= page.total; previous.disabled = !offset;
    if (!page.items.length) rows.append(empty('还没有匹配的资料。附件、项目知识和运行成果会显示在这里。'));
  }
  let timer;
  query.oninput = () => { clearTimeout(timer); timer = setTimeout(attempt(async () => { offset = 0; await load(); }), 160); };
  kind.onchange = attempt(async () => { offset = 0; await load(); });
  body.append(node('div', {class: 'toolbar'}, query, kind), rows, node('div', {class: 'toolbar'}, status, previous, next));
  await load();
}

export async function inspector(api, session, host, tab, actions) {
  host.replaceChildren(); if (!session) { host.append(empty('选择一个会话后查看任务、资料与记忆')); return; }
  const refresh = actions.refresh;
  if (tab === 'memory') { await memoryPanel(api, session, host, {refresh, compact: true}); return; }
  if (tab === 'materials') {
    const page = await api.operation('materials.list', {session: session.id, limit: 50});
    for (const item of page.items) host.append(button([node('span', {}, '▤ ' + item.title), node('small', {}, labels[item.kind] || item.kind)],
      attempt(() => openMaterial(api, session, item, actions)), 'material-row'));
    if (!page.items.length) host.append(empty('当前会话还没有资料'));
    host.append(button('打开资料与记忆', actions.library, 'text-button')); return;
  }
  const tasks = session.state?.todos || [], completed = session.state?.recent_completed_todos || [];
  host.append(node('div', {class: 'toolbar'}, node('h3', {}, '任务'), button('＋ 添加', () => form('添加任务', [{name: 'title', label: '任务', required: true}], {}, async ({title}) => {
    await api.operation('sessions.tasks', {session: session.id, operations: [{action: 'create', title}], expected_revision: session.revision}); closeModal(); await refresh();
  }))));
  for (const task of tasks) host.append(node('div', {class: 'task-row'}, node('span', {class: 'task-status'}, task.status === 'in_progress' ? '◐' : '○'),
    node('div', {}, node('strong', {}, task.title), task.note ? node('small', {}, task.note) : null),
    button('完成', attempt(async () => { await api.operation('sessions.tasks', {session: session.id, operations: [{action: 'update', id: task.id, status: 'completed'}], expected_revision: session.revision}); await refresh(); })),
    button('删除', attempt(async () => { await api.operation('sessions.tasks', {session: session.id, operations: [{action: 'delete', id: task.id}], expected_revision: session.revision}); await refresh(); }))));
  if (!tasks.length) host.append(empty('暂无进行中的任务'));
  if (completed.length) host.append(node('details', {}, node('summary', {}, '最近完成 · ' + completed.length),
    completed.map(item => node('p', {class: 'muted'}, '✓ ' + item.title))));
  const processes = await api.operation('processes.list', {session: session.id});
  host.append(node('h3', {}, '后台进程'));
  for (const item of processes) host.append(node('div', {class: 'setting-row'}, node('div', {}, node('strong', {}, item.command || item.name || item.id),
    node('small', {}, item.status || '运行中')), button('停止', attempt(async () => { await api.operation('processes.stop', {session: session.id, process: item.id || item.process_id}); await refresh(); }))));
  if (!processes.length) host.append(empty('没有后台进程'));
}

let traceWindow;
export async function trace(api, session, perform, tab = 'events') {
  if (!traceWindow) {
    traceWindow = node('dialog', {id: 'run-dialog', 'aria-label': '运行检查'});
    document.body.append(traceWindow);
  }
  const close = () => traceWindow.close(), rows = node('div', {class: 'trace-list'}), detail = node('div', {class: 'trace-detail'});
  const tabs = node('div', {class: 'settings-tabs'},
    button('过程', attempt(() => trace(api, session, perform, 'events')), tab === 'events' ? 'active' : ''),
    button('当前状态', attempt(() => trace(api, session, perform, 'state')), tab === 'state' ? 'active' : ''));
  traceWindow.replaceChildren(node('div', {class: 'dialog-header'}, node('div', {}, node('h2', {}, '运行检查'), node('small', {}, session.title || '当前会话')),
    button('最大化 / 还原', () => traceWindow.classList.toggle('maximized')), button('×', close, 'icon-button')),
    node('div', {class: 'trace-body'}, tabs, node('div', {class: 'trace-layout'}, rows, detail)));
  if (!traceWindow.open) traceWindow.show();
  const heading = traceWindow.querySelector('.dialog-header');
  heading.onpointerdown = event => {
    if (event.target.closest('button') || traceWindow.classList.contains('maximized')) return;
    const initial = traceWindow.getBoundingClientRect(), origin = {x: event.clientX, y: event.clientY}; heading.setPointerCapture(event.pointerId);
    heading.onpointermove = move => { traceWindow.style.left = Math.max(0, Math.min(innerWidth - 150, initial.left + move.clientX - origin.x)) + 'px'; traceWindow.style.top = Math.max(0, Math.min(innerHeight - 60, initial.top + move.clientY - origin.y)) + 'px'; traceWindow.style.margin = '0'; };
    heading.onpointerup = () => { heading.onpointermove = null; };
  };
  if (tab === 'state') {
    const value = await api.operation('sessions.read', {session: session.id, limit: 1});
    const state = value.state || {};
    rows.append(node('p', {}, value.mode + ' · ' + value.model), node('p', {class: 'muted'}, '状态版本 ' + (state.state_version || 0)),
      button('会话摘要', () => detail.replaceChildren(markdown(state.summary || '暂无压缩摘要'))),
      button('任务 · ' + (state.todos?.length || 0), () => detail.replaceChildren(...(state.todos || []).map(item => node('div', {class: 'task-row'}, item.title, node('small', {}, item.status))))),
      button('成果 · ' + Object.keys(state.artifacts || {}).length, () => detail.replaceChildren(...Object.values(state.artifacts || {}).map(item => button(item.name,
        attempt(() => openMaterial(api, value, {ref: {kind: 'artifact', ref: 'artifact:' + item.name, name: item.name}}, {perform})), 'material-row')))),
      button('查看状态数据', () => detail.replaceChildren(node('pre', {class: 'data-reader'}, JSON.stringify(state, null, 2)))));
    detail.append(markdown(state.summary || '当前状态与历史调用记录分别保存。选择左侧项目查看。')); return;
  }
  let cursor = 0, entries = new Map();
  const more = button('加载更多', attempt(load)), search = node('input', {placeholder: '筛选调用 / 状态', 'aria-label': '筛选运行记录'});
  rows.append(search); const list = node('div'); rows.append(list, more);
  async function inspect(event) {
    if (!event.node_id) { detail.replaceChildren(node('pre', {class: 'data-reader'}, JSON.stringify(event, null, 2))); return; }
    const value = await api.operation('sessions.trace-node', {session: session.id, node: event.node_id, run_id: event.request_id || ''});
    detail.replaceChildren(...[node('h3', {}, event.name || event.tool_name || event.kind), node('p', {class: 'muted'}, [event.status, event.source, event.duration_ms ? event.duration_ms + ' ms' : ''].filter(Boolean).join(' · ')),
      value.note ? node('p', {class: 'notice'}, value.note) : null,
      node('details', {open: ''}, node('summary', {}, '参数与结果'), node('pre', {class: 'data-reader'}, JSON.stringify(value.tool || value.request || value, null, 2)))].filter(Boolean));
    if (value.tool?.name && value.tool.arguments_status === 'ok') detail.append(button('复测工具', attempt(() => perform('tools.call', {name: value.tool.name, arguments: value.tool.arguments, session: session.id}))),
      button('复制调用参数', attempt(async () => { await navigator.clipboard.writeText(JSON.stringify({name: value.tool.name, arguments: value.tool.arguments, session: session.id}, null, 2)); toast('已复制'); })));
  }
  function render() {
    const query = search.value.toLowerCase();
    const values = [...entries.values()].filter(event => JSON.stringify([event.name, event.tool_name, event.kind, event.status, event.source]).toLowerCase().includes(query));
    list.replaceChildren(...values.map(event => button([node('span', {}, event.name || event.tool_name || event.kind || '运行'), node('small', {}, [event.status, event.duration_ms ? event.duration_ms + ' ms' : ''].filter(Boolean).join(' · '))], attempt(() => inspect(event)), 'trace-row')));
    if (!values.length) list.append(empty('暂无匹配的运行记录'));
  }
  async function load() {
    const page = await api.operation('sessions.trace', {session: session.id, cursor, limit: 200});
    cursor = page.next_cursor;
    for (const event of page.events) {
      const key = (event.request_id || '') + ':' + (event.node_id || JSON.stringify(event));
      if (entries.has(key) || entries.size < 2000) entries.set(key, {...entries.get(key), ...event});
    }
    more.disabled = !page.has_more || entries.size >= 2000;
    render(); if (page.note) detail.replaceChildren(node('p', {class: 'notice'}, page.note));
  }
  search.oninput = render; detail.append(empty('选择一项调用查看输入、结果和复测入口')); await load();
}

export async function contextDetails(api, session, perform) {
  const value = await api.operation('sessions.context', {session: session.id}), budget = value.budget;
  const entries = [['当前输入', value.context_tokens], ['可用输入预算', budget.effective_prompt_limit], ['输出预留', budget.output_limit],
    ['模型上下文窗口', budget.context_window], ['压缩阈值', budget.compact_threshold_tokens], ['参与上下文的消息', value.active_messages]];
  modal('上下文预算', [node('p', {class: 'muted'}, '当前会话估算 · ' + (value.usage_ratio * 100).toFixed(1) + '%'),
    node('div', {class: 'metric-list'}, entries.map(([name, count]) => node('div', {class: 'setting-row'}, node('span', {}, name), node('strong', {}, Number(count).toLocaleString()))))],
    [button('关闭', closeModal), button('压缩上下文', attempt(() => perform('sessions.compact', {session: session.id, expected_revision: session.revision})))]);
}
