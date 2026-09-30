// The host exposes only its UI language and translations from pycat_en.ts.
// Raw messages, tool results, identifiers and editable data never pass here.
let language = 'zh_CN', messages = {};

export function useLanguage(catalog) {
  language = catalog.language === 'en' ? 'en' : 'zh_CN';
  messages = language === 'en' ? catalog.messages || {} : {};
}

export function tr(source, values = {}) {
  return (Object.hasOwn(messages, source) ? messages[source] : source).replace(/\{([a-zA-Z_][a-zA-Z_0-9]*)\}/g,
    (match, key) => Object.hasOwn(values, key) ? String(values[key]) : match);
}

export function translateDocument(root = document) {
  root.documentElement.lang = language === 'en' ? 'en' : 'zh-CN';
  for (const item of root.querySelectorAll('[data-i18n]')) item.textContent = tr(item.dataset.i18n);
  for (const attribute of ['title', 'placeholder', 'aria-label', 'data-prompt']) {
    for (const item of root.querySelectorAll(`[data-i18n-${attribute}]`))
      item.setAttribute(attribute, tr(item.getAttribute(`data-i18n-${attribute}`)));
  }
}

// A leaf module loads first so translated module-level menus do not freeze in
// the fallback language. Node-based contract checks may load it without a DOM.
if (typeof window !== 'undefined') {
  try {
    const response = await fetch('/ui-language.json', {credentials: 'omit'});
    if (response.ok) useLanguage(await response.json());
  } catch { /* Source text remains usable when the host is unavailable. */ }
}
