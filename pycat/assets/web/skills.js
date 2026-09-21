import {resourceBrowser, resourceRow} from './resources.js';
import {node, button, modal, closeModal, form, markdown, toast, reader} from './ui.js';

export async function renderSkills(w, body) {
  const work_dir = w.session?.work_dir || '';
  const items = await w.api.operation('skills.list', {work_dir});
  const search = node('input', {placeholder: '搜索技能', 'aria-label': '搜索技能'});
  const list = node('div', {class: 'resource-list'}), detail = node('div', {class: 'resource-detail'});
  const browser = resourceBrowser(list, detail);
  const scopeOptions = [{value: 'global', label: '用户技能'}, ...(work_dir ? [{value: 'project', label: '项目技能'}] : [])];
  const context = item => ({name: item.name, work_dir, scope: item.source_scope === 'project' ? 'project' : 'global'});
  async function open(item, reveal = true) {
    browser.open(item.name, reveal);
    detail.replaceChildren(node('h3', {}, item.name), node('p', {class: 'muted'}, item.description),
      node('small', {}, ({project: '项目技能', global: '用户技能', bundled: '内置技能', external: '外部技能'}[item.source_scope] || item.source_scope) + (item.read_only ? ' · 只读' : '') + ' · ' + (item.enabled ? '已启用' : '已停用')));
    detail.append(button('版本与更新', () => w.showResourceUpdate(item.name)));
    if (item.read_only) detail.append(node('div', {class: 'toolbar'},
      button('复制为用户技能', event => w.action(async () => { await w.api.operation('skills.copy', {name: item.name, work_dir}); await w.navigate('skills'); }, event.currentTarget)),
      item.source_scope === 'bundled' ? button(item.enabled ? '停用' : '启用', event => w.action(async () => {
        await w.api.operation('skills.enable', {name: item.name, work_dir, enabled: !item.enabled}); await w.navigate('skills');
      }, event.currentTarget)) : null));
    if (!item.read_only) detail.append(node('div', {class: 'toolbar'},
      button('编辑', () => form('编辑技能', [{name: 'description', label: '用途'}, {name: 'content', label: '指令内容', type: 'text', required: true}],
        item, async values => { await w.api.operation('skills.save', {...context(item), ...values}); closeModal(); toast('技能已保存'); await w.navigate('skills'); })),
      button(item.enabled ? '停用' : '启用', event => w.action(async () => { await w.api.operation('skills.enable', {name: item.name, work_dir, enabled: !item.enabled}); await w.navigate('skills'); }, event.currentTarget)),
      button('管理资源', () => form('技能资源', [{name: 'relative_path', label: '技能内路径', required: true}, {name: 'content', label: '资源内容', type: 'text'}, {name: 'remove', label: '删除此资源', type: 'bool', default: false}],
        {relative_path: '', content: '', remove: false}, async values => { await w.api.operation('skills.resource', {...context(item), ...values}); toast('资源已更新'); closeModal(); })),
      button('删除', () => modal('删除技能', node('p', {}, '删除用户维护的“' + item.name + '”？'), [
        button('取消', closeModal), button('删除', event => w.action(async () => { await w.api.operation('skills.delete', {name: item.name, work_dir}); closeModal(); await w.navigate('skills'); }, event.currentTarget), 'danger')])))
    );
    detail.append(markdown(item.content));
  }
  const scopeLabel = item => ({project: '项目', global: '个人', bundled: '内置', external: '外部'}[item.source_scope] || item.source_scope);
  const render = () => list.replaceChildren(...items.filter(item => (item.name + ' ' + item.description).toLowerCase().includes(search.value.toLowerCase())).map(item =>
    resourceRow({title: item.name, description: item.description, meta: scopeLabel(item) + (item.enabled ? ' · 已启用' : ' · 已停用'),
      enabled: item.enabled, open: () => open(item), toggle: !item.read_only || item.source_scope === 'bundled' ? event => w.action(async () => {
        await w.api.operation('skills.enable', {name: item.name, work_dir, enabled: !item.enabled}); await w.navigate('skills');
      }, event.currentTarget) : null})));
  search.oninput = render; render();
  body.append(
    node('div', {class: 'toolbar'}, search,
      button('＋ 创建技能', () => form('创建技能', [{name: 'name', label: '名称', required: true}, {name: 'description', label: '用途'}, {name: 'scope', label: '范围', options: scopeOptions}],
        {name: '', description: '', scope: work_dir ? 'project' : 'global'}, async values => {
          await w.api.operation('skills.create', {...values, work_dir}); closeModal(); await w.navigate('skills');
        })),
      node('details', {class: 'resource-menu'}, node('summary', {}, '更多'), node('div', {},
      button('导入文件', () => {
        const input = node('input', {type: 'file', accept: '.zip,.md'});
        input.onchange = () => {
          const file = input.files[0]; if (!file) return;
          form('导入 ' + file.name, [{name: 'scope', label: '范围', options: scopeOptions},
            {name: 'overwrite', label: '覆盖已有技能', type: 'bool', default: false}], {scope: work_dir ? 'project' : 'global'}, async values => {
            const params = new URLSearchParams({...values, work_dir, name: file.name});
            await w.api.json('/skills/import?' + params, {method: 'POST', body: file, raw: true});
            closeModal(); toast('技能已导入'); await w.navigate('skills');
          });
        };
        input.click();
      }),
      button('从宿主目录导入', () => form('导入技能', [{name: 'path', label: '宿主上的技能目录 / ZIP / SKILL.md', required: true},
        {name: 'scope', label: '范围', options: scopeOptions}, {name: 'overwrite', label: '覆盖已有技能', type: 'bool', default: false}],
        {scope: work_dir ? 'project' : 'global'}, async values => { await w.api.operation('skills.import', {...values, work_dir}); closeModal(); await w.navigate('skills'); })),
      button('待审核', event => w.action(async () => {
        const candidates = await w.api.operation('skills.candidates', {work_dir});
        const rows = node('div');
        for (const item of candidates) rows.append(button([node('span', {}, item.name), node('small', {}, item.status || '待审核')], () => w.action(async () => {
          const proposal = item.id, scope = item.scope;
          const value = await w.api.operation('skills.candidate', {proposal, scope, work_dir});
          modal('技能变更 · ' + item.name, [markdown(value.after || ''), node('details', {}, node('summary', {}, '修改前'), markdown(value.before || '新建技能')), node('details', {}, node('summary', {}, '评测记录'), node('pre', {class: 'data-reader'}, JSON.stringify(value.evaluation || {}, null, 2)))], [
            button('关闭', closeModal),
            button('评测', () => w.context.perform?.('skills.evaluate', {proposal, scope, session: w.session?.id})),
            button('发布', event => w.action(async () => { await w.api.operation('skills.publish', {proposal, scope, work_dir}); closeModal(); toast('已发布技能'); await w.navigate('skills'); }, event.currentTarget)),
            button('回滚', event => w.action(async () => { await w.api.operation('skills.publish', {proposal, scope, work_dir, rollback: true}); closeModal(); toast('已回滚'); await w.navigate('skills'); }, event.currentTarget))
          ]);
        }), 'picker-row'));
        if (!candidates.length) rows.append(node('p', {class: 'empty-small'}, '没有待审核的技能变更'));
        modal('待审核技能', rows, [button('关闭', closeModal)]);
      }, event.currentTarget))))),
    browser.element);
  if (items.length) open(items[0], false);
  if (!items.length) list.append(node('p', {class: 'empty-small'}, '还没有技能。创建一项或从目录导入。'));
}
