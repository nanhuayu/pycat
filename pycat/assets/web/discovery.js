import {resourceBrowser, resourceRow} from './resources.js';
import {node, button, form, closeModal} from './ui.js';

export async function renderResourceDiscovery(w, body, kind, renderInstalled) {
  const tabs = w.resourceTabs, content = node('div');
  body.append(content);
  w.resourceMode ||= {};
  async function show(mode, installedId = '') {
    w.disposePage?.(); w.disposePage = null;
    w.resourceMode[kind] = mode;
    tabs.replaceChildren(button('已安装', () => show('installed'), mode === 'installed' ? 'active' : ''),
      button('市场', () => show('market'), mode === 'market' ? 'active' : ''));
    const host = node('div'); content.replaceChildren(host);
    try {
      if (mode === 'market') await renderDiscovery(w, host, kind, installedId);
      else await renderInstalled(w, host);
    } catch (error) { if (host.isConnected) host.replaceChildren(node('p', {class: 'form-error'}, error.message)); }
  }
  w.showResourceUpdate = id => show('market', id);
  const requested = w.discoveryTarget;
  w.discoveryTarget = null;
  await show(requested ? 'market' : w.resourceMode[kind] || 'installed', requested || '');
}

async function renderDiscovery(w, body, kind, installedId) {
  const work_dir = w.session?.work_dir || '';
  const catalog = () => w.api.operation('extensions.list', {work_dir, servers: w.draft.mcp_servers});
  const localRows = items => items.filter(row => row.kind === kind && (!row.installed || row.id === 'agent-browser' || row.id === installedId));
  let rows = localRows(await catalog()), market = false, nextCursor = '', marketQuery = '';
  if (!body.isConnected) return;
  const plans = new Map();
  let selected = null, busy = false, disposed = false, cancelled = false, runId = null;
  const status = node('p', {class: 'notice', role: 'status'}, '搜索市场或检查更新时才会联网。');
  const cancel = button('取消', () => stop()); cancel.hidden = true;
  const search = node('input', {placeholder: kind === 'mcp' ? '搜索 MCP' : '搜索 Skills', 'aria-label': '市场搜索词'});
  const list = node('div', {class: 'resource-list'}), detail = node('div', {class: 'resource-detail'});
  const browser = resourceBrowser(list, detail);
  const key = row => row.kind + ':' + row.id;
  const alive = () => !disposed && body.isConnected;
  async function stop() {
    cancelled = true;
    if (runId) await w.api.json('/runs/' + runId + '/cancel', {method: 'POST'}).catch(() => {});
  }
  w.disposePage = () => { disposed = true; void stop(); };
  async function operation(name, args) {
    const job = await w.api.operation(name, args);
    runId = job.run_id;
    if (cancelled || !alive()) { await stop(); throw Error('操作已取消'); }
    for await (const event of w.api.events(runId)) if (event.type === 'final') {
      runId = null;
      if (event.status !== 'completed') throw Error(event.error || '操作已取消');
      if (cancelled || !alive()) throw Error('操作已取消');
      return event.data;
    }
    throw Error('没有收到操作结果，请检查安装状态后重试。');
  }
  async function perform(action, message) {
    if (busy) return;
    busy = true; cancelled = false; status.textContent = message; cancel.hidden = false;
    renderList(); if (selected) open(selected);
    try { await action(); }
    catch (error) { if (alive()) status.textContent = error.message; }
    finally { busy = false; runId = null; if (alive()) { cancel.hidden = true; renderList(); if (selected) open(selected); } }
  }
  function renderList() {
    const query = market ? '' : search.value.toLowerCase();
    const visible = rows.filter(row => (row.title + ' ' + row.description + ' ' + row.source).toLowerCase().includes(query));
    list.replaceChildren(...visible.map(row => resourceRow({title: row.title, description: row.description,
      meta: row.status + ' · ' + row.source, open: () => open(row, true)})));
    if (!visible.length) list.append(node('p', {class: 'empty-small'}, '没有匹配的条目，请输入关键词搜索市场。'));
  }
  async function refresh() {
    if (!market) rows = localRows(await catalog());
    if (selected) selected = rows.find(row => key(row) === key(selected)) || selected;
  }
  function open(row, show = false) {
    browser.open(row.title, show || browser.element.dataset.detail === "open");
    selected = row;
    const plan = plans.get(key(row));
    const ownership = {bundled: '随 PyCat 更新；可在 Skills 页停用或复制后编辑。',
      managed: 'PyCat 管理来源。更新保留配置；技能有本地修改时会停止更新。',
      external: '外部管理。请在原来源更新程序或重新导入技能；刷新工具列表不会升级程序。',
      directory: '查看在线目录，再按需配置 MCP 或安装技能。',
      market: '公开市场条目。添加 MCP 为停用草稿；技能安装前会核对 GitHub 目录和固定提交。'}[row.management];
    detail.replaceChildren(node('h3', {}, row.title), node('p', {}, row.description), node('p', {class: 'muted'}, ownership),
      node('p', {}, '来源：' + row.source), node('p', {}, '当前版本：' + (row.current_version || '未记录')));
    if (row.kind === 'mcp' && row.id === 'agent-browser') detail.append(node('p', {class: 'muted'},
      '本机浏览器：' + (row.browser_path || '未找到；请先安装 Chrome / Edge / Chromium，或在 MCP 环境变量中指定程序。')));
    if (plan) detail.append(node('p', {}, '可安装版本：' + plan.version));
    if (plan?.sha256) detail.append(node('p', {class: 'muted'}, 'SHA-256：' + plan.sha256));
    if (row.market_version) detail.append(node('p', {}, '市场版本：' + row.market_version));
    const actions = node('div', {class: 'toolbar'});
    if (row.website) actions.append(node('a', {href: row.website, target: '_blank', rel: 'noopener noreferrer'}, '查看来源'));
    if ((row.management === 'managed' || row.management === 'market' && kind === 'skill') && row.supported !== false) actions.append(button(row.management === 'market' && !row.installed ? '准备安装' : '检查更新', () => perform(async () => {
      const result = row.management === 'market' && !row.installed
        ? await operation('extensions.market-skill', {repository: row.repository, name: row.skill_name})
        : await operation('extensions.check', {id: row.skill_name || row.id, kind: row.kind, work_dir});
      if (!alive()) return;
      plans.set(key(row), result);
      status.textContent = result.version === row.current_version ? '已是最新版本。' : '版本已确认，点击安装或更新以继续。';
    }, '正在检查官方来源…')));
    if (plan && plan.version !== row.current_version) actions.append(button(row.current_version ? '更新' : '安装', () => perform(async () => {
      if (row.kind === 'mcp') {
        const configuration = w.draft.mcp_servers.find(item => item.integration === 'agent-browser');
        if (!configuration && w.draft.mcp_servers.some(item => item.name === 'browser')) throw Error('已存在名为 browser 的 MCP，请先重命名该服务。');
        const result = await operation('extensions.prepare-browser', {plan, configuration: configuration || null});
        if (!alive()) return;
        const index = w.draft.mcp_servers.findIndex(item => item.name === result.name);
        if (index < 0) w.draft.mcp_servers.push(result); else w.draft.mcp_servers[index] = result;
        w.changed(); status.textContent = '驱动已验证，MCP 配置已加入草稿。保存设置后生效。';
      } else {
        await operation('extensions.install-skill', {plan, work_dir, scope: plan.scope || 'global', overwrite: !!row.current_version});
        if (!alive()) return;
        row.installed = true; row.current_version = plan.version; row.status = '已安装';
        status.textContent = '技能已安装并生效，来源和提交已记录。';
      }
      await refresh();
    }, '正在下载、校验并安装…'), 'primary'));
    if (row.options?.length) {
      const options = node('select', {'aria-label': 'MCP 接入方式'}, row.options.map((option, index) => node('option', {value: index}, option.label)));
      detail.append(options);
      const add = button(row.installed ? '已添加' : '添加配置', () => perform(async () => {
        const config = await w.api.operation('mcp.validate', {configuration: row.options[Number(options.value)].configuration});
        if (!alive()) return;
        if (w.draft.mcp_servers.some(item => item.name === config.name)) throw Error('已存在同名 MCP，请在已安装页查看或修改名称。');
        w.draft.mcp_servers.push(config); w.changed();
        row.installed = true; row.status = '已配置';
        status.textContent = '已加入停用草稿；在已安装页填写凭据、测试连接并启用，保存设置后生效。';
      }, '正在准备配置…'));
      add.disabled = !!row.installed; actions.append(add);
    } else if (row.management === 'market' && kind === 'mcp') detail.append(node('p', {class: 'muted'}, '此条目需要自定义安装步骤，请查看来源后手动添加配置。'));
    for (const control of actions.querySelectorAll('button')) control.disabled ||= busy;
    detail.append(actions);
  }
  const importButton = button('从 GitHub 安装 Skill', () => form('从 GitHub 安装 Skill', [
    {name: 'repository', label: 'GitHub 仓库（owner/repo）', required: true},
    {name: 'path', label: '技能目录（skills/example-skill）', required: true},
    {name: 'ref', label: '分支 / 标签 / 提交', required: true},
    {name: 'scope', label: '安装到', options: [{value: 'global', label: '用户技能'}, ...(work_dir ? [{value: 'project', label: '当前项目'}] : [])]}
  ], {ref: 'HEAD', scope: 'global'}, values => {
    closeModal(); void perform(async () => {
      const plan = await operation('extensions.skill-preview', {repository: values.repository, path: values.path, ref: values.ref});
      if (!alive()) return;
      plan.scope = values.scope; const row = {id: plan.name, kind: 'skill', title: plan.name, management: 'managed',
        source: plan.repository, website: 'https://github.com/' + plan.repository, description: plan.path, status: '待安装', current_version: ''};
      plans.set(key(row), plan); rows = rows.filter(item => key(item) !== key(row)); rows.push(row); selected = row; browser.open();
      status.textContent = '已固定提交，请核对来源后点击安装。';
    }, '正在检查技能来源…');
  }));
  const searchMarket = (next = false) => perform(async () => {
    const query = next ? marketQuery : search.value;
    const result = await operation('extensions.search', {kind, query, cursor: next ? nextCursor : '', work_dir, servers: w.draft.mcp_servers});
    if (!alive()) return;
    rows = result.items; market = true; marketQuery = query; nextCursor = result.next_cursor;
    selected = rows[0] || null; nextButton.hidden = !nextCursor;
    if (!selected) detail.replaceChildren(node('p', {class: 'empty-small'}, '没有匹配结果'));
    status.textContent = '找到 ' + rows.length + ' 项。目录登记不代表 PyCat 已验证该程序，请核对来源与依赖。';
  }, '正在搜索公开市场…');
  const searchButton = button('搜索市场', () => searchMarket());
  const resetButton = button('推荐', () => perform(async () => { rows = localRows(await catalog()); market = false; search.value = ''; selected = rows[0] || null; nextButton.hidden = true; }, '正在读取本地推荐…'));
  const nextButton = button('下一页', () => searchMarket(true)); nextButton.hidden = true;
  importButton.hidden = kind !== 'skill';
  search.oninput = renderList; search.onkeydown = event => { if (event.key === 'Enter') { event.preventDefault(); searchMarket(); } };
  body.append(node('div', {class: 'toolbar'}, search, searchButton, resetButton, importButton, nextButton),
    browser.element, node('div', {class: 'toolbar'}, status, cancel));
  renderList(); if (rows.length) open(rows.find(row => row.id === installedId) || rows[0], !!installedId);
}
