import {node, button} from './ui.js';

export const option = (value, label = value) => ({value, label});
export const categories = [['read', '读取与搜索'], ['web', '联网请求'], ['edit', '修改文件'], ['execute', '终端与进程'], ['state', '状态与资料'], ['delegate', '委派 Agent'], ['capability', '模型能力'], ['mcp', 'MCP 工具']];
export const getPath = (object, path) => path.split('.').reduce((value, key) => value?.[key], object);
export function setPath(object, path, value) {
  const keys = path.split('.'), last = keys.pop();
  for (const key of keys) object = object[key] ??= {};
  object[last] = value;
}

// One editor for settings drafts and resource forms. Only the caller commits.
export function fieldControl(field, value, change) {
  const type = field.type || 'str';
  const row = node('label', {class: 'setting-field' + (['text', 'json', 'lines'].includes(type) ? ' stacked' : '')});
  row.append(node('span', {class: 'field-label'}, field.label, field.help ? node('small', {}, field.help) : null));
  let input;
  if (field.options) input = node('select', {}, field.options.map(item => node('option', {value: item.value ?? item}, item.label ?? item)));
  else if (type === 'bool') input = node('input', {type: 'checkbox', role: 'switch'});
  else if (type === 'text' || type === 'json' || type === 'lines') input = node('textarea', {rows: type === 'json' ? 5 : 3, spellcheck: 'false'});
  else input = node('input', {type: field.secret ? 'password' : ['int', 'float'].includes(type) ? 'number' : 'text', autocomplete: 'off'});
  input.setAttribute('aria-label', field.label);
  if (field.min !== undefined) input.min = field.min;
  if (field.max !== undefined) input.max = field.max;
  if (type === 'float') input.step = 'any';
  if (type === 'bool') input.checked = !!value;
  else input.value = field.secret && value === '__secret__' ? '' : value == null ? '' : type === 'json' ? JSON.stringify(value, null, 2) : type === 'lines' ? value.join('\n') : value;
  if (field.secret && value === '__secret__') input.placeholder = '已保存，留空保留';
  else input.placeholder = field.placeholder || (field.nullable ? '继承默认值' : '');
  const update = () => {
    let next;
    input.setCustomValidity('');
    try {
      if (type === 'bool') next = input.checked;
      else if (field.secret && !input.value && value === '__secret__' && !input.dataset.cleared) next = value;
      else if (field.nullable && !input.value.trim()) next = null;
      else if (type === 'json') next = JSON.parse(input.value || '{}');
      else if (type === 'lines') next = input.value.split('\n').map(item => item.trim()).filter(Boolean);
      else if (type === 'int' || type === 'float') {
        next = Number(input.value);
        if (!input.value || !Number.isFinite(next) || type === 'int' && !Number.isInteger(next)) throw Error('请输入有效数字');
      } else next = input.value;
      change(next);
    } catch (error) { input.setCustomValidity(error.message); }
  };
  input.addEventListener('input', update); input.addEventListener('change', update);
  const control = node('div', {class: 'field-control'}, input);
  if (field.secret) control.append(button('清除', () => { input.value = ''; input.dataset.cleared = '1'; update(); }, 'text-button'));
  row.append(control); return row;
}
export function fieldset(title, description, object, fields, change = () => {}) {
  const section = node('section', {class: 'settings-section'}, node('h3', {}, title));
  if (description) section.append(node('p', {class: 'muted'}, description));
  section.append(...fields.map(field => fieldControl(field, getPath(object, field.key), value => { setPath(object, field.key, value); change(field.key, value); })));
  return section;
}
export function choices(label, values, selected, change) {
  return node('fieldset', {class: 'check-options'}, node('legend', {}, label), values.map(([value, title]) => {
    const input = node('input', {type: 'checkbox'}); input.checked = selected.includes(value);
    input.onchange = () => change([...input.closest('fieldset').querySelectorAll('input:checked')].map(item => item.value)); input.value = value;
    return node('label', {}, input, title);
  }));
}
