import {node, button, modal, closeModal, toast} from './ui.js';
import {renderSettingsPage, groups} from './settings-pages.js';

let workspace = null;
let context = {};
export const settingsActive = () => workspace !== null;
export const configureSettings = value => { context = value; };
export async function showConfig(api, refresh, page = 'models') {
  page = ({providers: 'models', app_settings: 'general', search_config: 'search', mcp_servers: 'mcp', config: 'general'})[page] || page;
  if (!workspace) {
    const [snapshot, schema] = await Promise.all([api.operation('config.read'), api.operation('config.schema')]);
    workspace = new SettingsWorkspace(api, refresh, snapshot);
    workspace.schema = schema;
    document.body.append(workspace.element);
    document.body.classList.add('settings-open');
  }
  closeModal();
  await workspace.navigate(page);
}
export function closeSettings(after) {
  if (!workspace) { after?.(); return; }
  const close = () => {
    workspace.disposePage?.();
    workspace.element.remove(); workspace = null;
    document.body.classList.remove('settings-open'); closeModal();
    document.querySelector('#composer').focus(); after?.();
  };
  if (!workspace.dirty.length && !workspace.invalid) { close(); return; }
  modal('保存设置更改？', node('p', {}, '更改尚未保存。可以保存后返回，或放弃本次草稿。'), [
    button('继续编辑', closeModal), button('放弃更改', close),
    button('保存并返回', async () => { closeModal(); if (await workspace.save()) close(); }, 'primary')
  ]);
}

class SettingsWorkspace {
  constructor(api, refresh, snapshot) {
    this.api = api; this.refresh = refresh; this.snapshot = snapshot;
    this.draft = structuredClone(snapshot.values); this.page = 'models'; this.version = 0;
    this.context = context; this.search = node('input', {placeholder: '搜索设置', 'aria-label': '搜索设置'});
    this.nav = node('nav', {'aria-label': '设置导航'});
    const brand = node('div', {class: 'brand'}, node('img', {src: '/brand.svg', alt: ''}), node('strong', {}, 'PyCat'),
      button('←', () => closeSettings(), 'brand-action'));
    brand.lastChild.setAttribute('aria-label', '返回会话');
    this.sidebar = node('aside', {class: 'settings-sidebar'}, brand, node('label', {class: 'search'}, this.search), this.nav);
    this.content = node('div', {class: 'settings-content'});
    this.error = node('p', {class: 'form-error', role: 'alert'});
    this.status = node('span', {class: 'muted'});
    this.discardButton = button('放弃更改', () => {
      modal('放弃全部设置更改？', node('p', {}, '尚未保存的配置将恢复为上次读取的状态。'), [
        button('继续编辑', closeModal), button('放弃更改', () => { this.draft = structuredClone(this.snapshot.values); closeModal(); this.navigate(this.page, true); }, 'danger')]);
    });
    this.saveButton = button('保存更改', () => this.save(), 'primary');
    this.element = node('section', {id: 'settings-layout', 'aria-label': '设置'},
      this.sidebar, node('div', {class: 'settings-workspace'},
        node('div', {class: 'settings-scroll'}, this.content),
        node('footer', {class: 'settings-save'}, this.error, node('div', {class: 'save-row'}, this.status, node('span', {}, this.discardButton, this.saveButton)))));
    this.search.oninput = () => this.renderNav();
    this.content.addEventListener('input', () => this.changed());
    this.element.addEventListener('keydown', event => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); this.save(); }
    });
    this.changed();
  }
  get dirty() { return Object.keys(this.draft).filter(key => JSON.stringify(this.draft[key]) !== JSON.stringify(this.snapshot.values[key])); }
  get invalid() { return this.element.querySelector(':invalid'); }
  get session() { return this.context.session?.(); }
  changed() {
    const dirty = this.dirty.length || !!this.invalid;
    this.status.textContent = this.invalid ? '输入尚未完成，请修正后保存' : dirty ? dirty + ' 个配置域有未保存更改' : '所有更改已保存';
    this.saveButton.disabled = !dirty || this.saving; this.discardButton.disabled = !dirty || this.saving;
    this.saveButton.textContent = '保存更改';
    this.saveButton.hidden = this.discardButton.hidden = this.resources && this.page === 'skills' && !dirty;
    if (this.resources && !dirty) this.status.textContent = this.page === 'skills' ? '技能操作即时生效' : 'MCP 配置保存后生效';
  }
  renderNav() {
    const query = this.search.value.trim().toLowerCase();
    this.nav.replaceChildren(...groups.filter(group => (group.title + group.pages.map(page => page[1]).join('')).toLowerCase().includes(query)).map(group => {
      const item = button(group.title, () => this.navigate(group.pages[0][0]), 'settings-nav-item' + (group.pages.some(page => page[0] === this.page) ? ' active' : ''));
      return item;
    }));
  }
  async navigate(page, discardInvalid = false) {
    if (this.invalid && !discardInvalid) { this.invalid.reportValidity(); return; }
    this.disposePage?.(); this.disposePage = null;
    this.resources = ['skills', 'mcp'].includes(page);
    const group = groups.find(group => group.pages.some(item => item[0] === page)) || groups[0];
    if (!this.resources && !group.pages.some(item => item[0] === page)) page = group.pages[0][0];
    this.page = page; const version = ++this.version;
    this.renderNav(); this.changed(); this.content.replaceChildren(node('div', {class: 'settings-title'},
      node('h1', {}, group.title),
      node('p', {class: 'muted'}, this.resources ? '管理技能、连接外部工具，或从市场发现新能力。' : group.description)));
    if (group.pages.length > 1) this.content.append(node('div', {class: 'settings-tabs'}, group.pages.map(([id, title]) =>
      button(title, () => this.navigate(id), id === page ? 'active' : ''))));
    this.resourceTabs = this.resources ? node('div', {class: 'settings-tabs resource-views'}) : null;
    if (this.resourceTabs) this.content.append(this.resourceTabs);
    const body = node('div', {class: 'settings-page'}); this.content.append(body);
    try { await renderSettingsPage(this, page, body); }
    catch (error) { if (version === this.version) body.replaceChildren(node('p', {class: 'form-error'}, error.message), button('重试', () => this.navigate(page))); }
  }
  async save() {
    if (this.saving) return false;
    const invalid = this.invalid;
    if (invalid) { invalid.reportValidity(); return false; }
    if (!this.dirty.length) return true;
    this.saving = true; this.element.classList.add('saving'); this.error.textContent = ''; this.changed();
    try {
      const patch = Object.fromEntries(this.dirty.map(key => [key, this.draft[key]]));
      const policy = this.draft.app_settings.context.compression_policy;
      if (policy.tight_replay_threshold_ratio >= policy.token_threshold_ratio) throw Error('工具压缩阈值必须低于上下文压缩阈值');
      const result = await this.api.operation('config.update', {patch, expected_revision: this.snapshot.revision});
      const saved = {providers: 'providers', app_settings: 'app_settings', mcp: 'mcp_servers', modes: 'modes', search: 'search_config'};
      for (const domain of result.saved_domains) {
        const key = saved[domain]; if (key) this.draft[key] = structuredClone(result.values[key]);
      }
      this.snapshot = {revision: result.revision, values: result.values};
      await this.refresh();
      if (!result.ok) {
        this.error.textContent = [...result.domain_errors, ...result.stage_errors].map(item => item[1]).join('\n');
        return false;
      }
      toast('设置已保存'); await this.navigate(this.page); return true;
    } catch (error) { this.error.textContent = error.message + '；草稿已保留。'; return false; }
    finally { this.saving = false; this.element.classList.remove('saving'); this.changed(); }
  }
  edit(title, item, fields, accept, extra) {
    const copy = structuredClone(item);
    const body = node('div', {class: 'resource-editor'});
    // Imported once with the page module; fields only mutate a temporary copy.
    for (const build of fields) body.append(build(copy));
    if (extra) body.append(extra(copy));
    const error = node('p', {class: 'form-error', role: 'alert'}); body.append(error);
    modal(title, body, [button('取消', closeModal), button('应用到草稿', async () => {
      const invalid = body.querySelector(':invalid'); if (invalid) { invalid.reportValidity(); return; }
      try { await accept(copy); this.changed(); closeModal(); await this.navigate(this.page); }
      catch (failure) { error.textContent = failure.message; }
    }, 'primary')]);
  }
  async action(work, control) {
    if (control) control.disabled = true;
    try { return await work(); } catch (error) { toast(error.message, true); return null; }
    finally { if (control) control.disabled = false; }
  }
}
document.addEventListener('keydown', event => {
  if (!workspace || document.querySelector('#dialog').open) return;
  if (event.key === 'Escape') { event.preventDefault(); closeSettings(); }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); workspace.search.focus(); }
});
window.addEventListener('beforeunload', event => { if (workspace && (workspace.dirty.length || workspace.invalid)) { event.preventDefault(); event.returnValue = ''; } });
