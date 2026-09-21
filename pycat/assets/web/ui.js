import MarkdownIt from './vendor/markdown-it.js';
export const $ = selector => document.querySelector(selector);
export function node(tag, attributes = {}, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (key === 'class') element.className = value;
    else if (key.startsWith('on')) element.addEventListener(key.slice(2), value);
    else if (key === 'text') element.textContent = value;
    else if (value !== undefined && value !== null) element.setAttribute(key, value);
  }
  element.append(...children.flat().filter(value => value !== undefined && value !== null));
  return element;
}
export const button = (label, action, cls = '') => node('button', {class: cls, onclick: action, type: 'button'}, label);
export function toast(text, error = false) {
  const element = $('#toast'); element.textContent = text; element.hidden = false; element.classList.toggle('error', error);
  clearTimeout(toast.timer); toast.timer = setTimeout(() => { element.hidden = true; }, error ? 10000 : 4500);
}
export function modal(title, content, actions = [], eyebrow = '') {
  $('#dialog').classList.remove('wide-dialog');
  $('#dialog-title').textContent = title; $('#dialog-eyebrow').textContent = eyebrow;
  $('#dialog-body').replaceChildren(...(Array.isArray(content) ? content : [content]));
  $('#dialog-actions').replaceChildren(...actions); if (!$('#dialog').open) $('#dialog').showModal();
}
export function closeModal() { $('#dialog').close(); }
export function picker(title, items, choose) {
  const input = node('input', {placeholder: '搜索…', class: 'picker-search', 'aria-label': '搜索'}), list = node('div', {class: 'picker-list'});
  const render = () => {
    const filtered = items.filter(item => (item.label + ' ' + (item.detail || '')).toLowerCase().includes(input.value.toLowerCase()));
    list.replaceChildren(...filtered.map(item => button([node('span', {}, item.label), node('small', {}, item.detail || '')], () => choose(item), 'picker-row')));
    if (!filtered.length) list.append(node('p', {class: 'empty-small'}, '没有匹配的项目'));
  };
  input.addEventListener('input', render); render(); modal(title, [input, list]); input.focus();
}
const renderer = new MarkdownIt({html: false, breaks: true, linkify: true});
const defaultLink = renderer.renderer.rules.link_open || ((tokens, index, options, env, self) => self.renderToken(tokens, index, options));
renderer.renderer.rules.link_open = (tokens, index, options, env, self) => {
  tokens[index].attrSet('target', '_blank'); tokens[index].attrSet('rel', 'noopener noreferrer');
  return defaultLink(tokens, index, options, env, self);
};
export function markdown(text) {
  const root = node('div', {class: 'markdown'});
  root.innerHTML = renderer.render(String(text || ''));
  for (const block of root.querySelectorAll('pre')) {
    const copy = button('复制代码', async () => { await navigator.clipboard.writeText(block.querySelector('code')?.textContent || ''); toast('代码已复制'); }, 'code-copy');
    block.append(copy);
  }
  return root;
}
export function reader(title, value) {
  modal(title, typeof value === 'string' ? markdown(value) : node('pre', {class: 'data-reader'}, JSON.stringify(value, null, 2)), [button('关闭', closeModal)]);
}
export function form(title, fields, values, submit, {label = '保存', description = ''} = {}) {
  const body = node('form', {class: 'fields'}), controls = new Map(), error = node('p', {class: 'form-error', role: 'alert'});
  if (description) body.append(node('p', {class: 'muted'}, description));
  for (const field of fields) {
    const value = values[field.name] ?? field.default;
    const type = field.type || 'str';
    let input;
    if (field.options) input = node('select', {}, field.options.map(option => node('option', {value: option.value ?? option}, option.label ?? option)));
    else if (type === 'bool') input = node('input', {type: 'checkbox'});
    else if (['dict', 'list', 'text'].includes(type)) input = node('textarea', {rows: type === 'text' ? 5 : 12, spellcheck: 'false'});
    else input = node('input', {type: field.secret ? 'password' : type.startsWith('int') || type.startsWith('float') ? 'number' : 'text', autocomplete: 'off'});
    if (type === 'bool') input.checked = Boolean(value);
    else input.value = field.secret && value === '__secret__' ? '' : value === null || value === undefined ? '' : typeof value === 'object' ? JSON.stringify(value, null, 2) : value;
    if (field.secret && value === '__secret__') input.placeholder = '已保存；留空保留';
    const row = node('label', {class: type === 'bool' ? 'field check-field' : 'field'}, node('span', {}, field.label || field.name), input);
    if (field.secret) row.append(button('清除', () => { input.value = ''; input.dataset.clear = 'true'; input.placeholder = '保存后将清除'; }, 'text-button'));
    if (field.help) row.append(node('small', {}, field.help));
    body.append(row); controls.set(field.name, {input, field, original: value});
  }
  body.append(error);
  const save = button(label, async () => {
    const data = {};
    try {
      for (const [name, {input, field, original}] of controls) {
        const type = field.type || 'str', text = input.value;
        if (field.secret && !text && original === '__secret__' && !input.dataset.clear) data[name] = '__secret__';
        else if (type === 'bool') data[name] = input.checked;
        else if (!text && !field.required && (field.default === null || field.default === undefined)) data[name] = null;
        else if (['dict', 'list'].includes(type)) data[name] = JSON.parse(text);
        else if (type.startsWith('int') || type.startsWith('float')) { data[name] = Number(text); if (!Number.isFinite(data[name])) throw Error('请输入有效数字'); }
        else data[name] = text;
        if (field.required && data[name] === '') throw Error((field.label || name) + '不能为空');
      }
      save.disabled = true; error.textContent = ''; await submit(data);
    } catch (failure) { error.textContent = failure.message; }
    finally { save.disabled = false; }
  }, 'primary');
  body.addEventListener('submit', event => { event.preventDefault(); save.click(); });
  modal(title, body, [button('取消', closeModal), save]);
  return {body, controls};
}
