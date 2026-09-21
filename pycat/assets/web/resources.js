import {node, button} from './ui.js';

// The same detail stays mounted across width changes; CSS selects one or two panes.
export function resourceBrowser(list, detail) {
  const back = button('← 返回列表', () => show(false), 'resource-back text-button');
  const detailPage = node('section', {class: 'resource-detail-page'}, back, detail);
  const element = node('div', {class: 'resource-browser'}, list, detailPage);
  function show(value, title) {
    element.dataset.detail = value ? 'open' : 'closed';
    if (title) for (const row of list.querySelectorAll('.resource-overview-row')) row.classList.toggle('active', row.dataset.title === title);
  }
  return {element, open: (title, reveal = true) => show(reveal, title), back: () => show(false)};
}

export function resourceRow({title, description = '', meta = '', enabled, toggle, open}) {
  const main = button([node('span', {class: 'resource-symbol', 'aria-hidden': true}, '◇'),
    node('span', {class: 'resource-summary'}, node('strong', {}, title),
      node('span', {class: 'resource-description'}, description), node('small', {}, meta))], open, 'resource-overview-main');
  const row = node('div', {class: 'resource-overview-row', 'data-title': title}, main);
  if (toggle) {
    const control = button('', toggle, 'resource-toggle');
    control.setAttribute('role', 'switch'); control.setAttribute('aria-checked', String(enabled));
    control.setAttribute('aria-label', (enabled ? '停用 ' : '启用 ') + title); row.append(control);
  }
  const more = button('›', open, 'resource-more'); more.setAttribute('aria-label', '查看 ' + title);
  row.append(more); return row;
}
