// Original UI vectors based on the GPT Image design language. Brand is assets/pycat.svg.
const paths = {
  "activity": "<path d=\"M3 12h4l3-8 4 16 3-8h4\"/>",
  "cat": "",
  "compose": "<path d=\"M10 4H4v16h16v-6M14 4l3-3 4 4-12 12H5v-4Zm0 0 4 4\"/>",
  "pin": "<path d=\"m9 3 12 12-4 1-3 4-4-6-6-4 4-3ZM10 14l-7 7\"/>",
  "archive": "<path d=\"M3 4h18v4H3Zm2 4v13h14V8M9 12h6\"/>",
  "plus": "<path d=\"M12 5v14M5 12h14\"/>",
  "search": "<circle cx=\"10.5\" cy=\"10.5\" r=\"6.5\"/><path d=\"m16 16 5 5\"/>",
  "folder": "<path d=\"M3 7V5h6l2 2h10v13H3Z\"/>",
  "file": "<path d=\"M5 3h9l5 5v13H5Z M14 3v6h5 M8 13h8M8 16h6\"/>",
  "chevron": "<path d=\"m9 5 7 7-7 7\"/>",
  "down": "<path d=\"m6 9 6 6 6-6\"/>",
  "up": "<path d=\"m6 15 6-6 6 6\"/>",
  "check": "<path d=\"m5 12 4 4L19 6\"/>",
  "circle": "<circle cx=\"12\" cy=\"12\" r=\"9\"/>",
  "checked": "<circle cx=\"12\" cy=\"12\" r=\"9\"/><path d=\"m7 12 3 3 6-6\"/>",
  "close": "<path d=\"m6 6 12 12M6 18 18 6\"/>",
  "panel": "<rect x=\"3\" y=\"4\" width=\"18\" height=\"16\" rx=\"2\"/><path d=\"M15 4v16\"/>",
  "sidebar": "<rect x=\"3\" y=\"4\" width=\"18\" height=\"16\" rx=\"2\"/><path d=\"M9 4v16\"/>",
  "settings": "<path d=\"m9 3-.6 3-2.6 1.5-2.8-.8-2 3.5 2.2 2v3L1 17.3l2 3.5 2.8-.8 2.6 1.5L9 24h4l.6-2.5 2.6-1.5 2.8.8 2-3.5-2.2-2.1v-3l2.2-2-2-3.5-2.8.8L13.6 6 13 3Z\" transform=\"translate(2 -1.5) scale(.85)\"/><circle cx=\"11.4\" cy=\"10\" r=\"3\"/>",
  "book": "<path d=\"M12 5C8 3 5 3 3 4v15c3-1 6-1 9 1 3-2 6-2 9-1V4c-2-1-5-1-9 1Zm0 0v15\"/>",
  "shield": "<path d=\"m12 3 8 3v6c0 4-4 7-8 9-4-2-8-5-8-9V6Z\"/><path d=\"m8 12 3 3 5-6\"/>",
  "arrow": "<path d=\"M12 20V4m-6 6 6-6 6 6\"/>",
  "stop": "<rect x=\"6\" y=\"6\" width=\"12\" height=\"12\" rx=\"1\" fill=\"currentColor\" stroke=\"none\"/>",
  "more": "<circle cx=\"5\" cy=\"12\" r=\"1\"/><circle cx=\"12\" cy=\"12\" r=\"1\"/><circle cx=\"19\" cy=\"12\" r=\"1\"/>",
  "copy": "<rect x=\"8\" y=\"3\" width=\"12\" height=\"14\" rx=\"2\"/><path d=\"M16 17v4H4V7h4\"/>",
  "refresh": "<path d=\"M20 10a8 8 0 1 0-2 8M20 4v6h-6\"/>",
  "external": "<path d=\"M14 3h7v7m0-7L10 14M10 3H3v18h18v-7\"/>",
  "terminal": "<rect x=\"3\" y=\"4\" width=\"18\" height=\"16\" rx=\"2\"/><path d=\"m7 8 4 4-4 4m6 0h4\"/>",
  "code": "<path d=\"m7 6-5 6 5 6m10-12 5 6-5 6M14 3l-4 18\"/>",
  "brain": "<path d=\"M12 4c-4-5-8 2-6 4-5 0-5 7-2 8-1 6 6 7 8 3 2 4 9 3 8-3 3-1 3-8-2-8 2-2-2-9-6-4Zm0 0v15M6 8l2 3m10-3-2 3\"/>",
  "clock": "<circle cx=\"12\" cy=\"12\" r=\"9\"/><path d=\"M12 6v6l4 2\"/>",
  "download": "<path d=\"M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5\"/>",
  "upload": "<path d=\"M12 16V4m-5 5 5-5 5 5M4 16v5h16v-5\"/>",
  "back": "<path d=\"M20 12H4m6-6-6 6 6 6\"/>",
  "edit": "<path d=\"m4 16 12-12 4 4L8 20H4Zm9-9 4 4\"/>",
  "trash": "<path d=\"M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7\"/>",
  "spark": "<path d=\"m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5ZM20 2v4m-2-2h4\"/>",
  "link": "<path d=\"m9 15 6-6m-7 3-2 2a4 4 0 0 0 6 6l3-3m1-5 2-2a4 4 0 0 0-6-6L9 7\"/>",
  "chart": "<path d=\"M5 20V10m7 10V4m7 16v-7\"/>",
  "maximize": "<rect x=\"5\" y=\"5\" width=\"14\" height=\"14\" rx=\"1\"/>",
  "minimize": "<path d=\"M5 12h14\"/>",
  "image": "<rect x=\"3\" y=\"3\" width=\"18\" height=\"18\" rx=\"2\"/><circle cx=\"8\" cy=\"8\" r=\"2\"/><path d=\"m3 18 6-6 4 4 4-7 4 6\"/>",
  "info": "<circle cx=\"12\" cy=\"12\" r=\"9\"/><path d=\"M12 11v6M12 7h.01\"/>",
  "alert": "<path d=\"m12 3 10 18H2Z M12 9v5m0 3h.01\"/>",
  "globe": "<circle cx=\"12\" cy=\"12\" r=\"9\"/><ellipse cx=\"12\" cy=\"12\" rx=\"4\" ry=\"9\"/><path d=\"M3 12h18\"/>",
  "plug": "<path d=\"M8 3v5m8-5v5M6 8h12v4a6 6 0 0 1-12 0Zm6 10v4\"/>",
  "cube": "<path d=\"m12 3 9 5v9l-9 5-9-5V8Zm0 10 9-5M3 8l9 5v9\"/>",
  "chat": "<path d=\"M3 4h18v13H9l-6 4Z\"/>",
  "sun": "<circle cx=\"12\" cy=\"12\" r=\"4\"/><path d=\"M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1 1m12 12 1 1M5 19l1-1M18 6l1-1\"/>",
  "moon": "<path d=\"M20 14A9 9 0 0 1 10 3a9 9 0 1 0 10 11Z\"/>",
  "flag": "<path d=\"M5 22V3h14l-3 5 3 5H5\"/>",
  "list": "<path d=\"M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01\"/>",
  "lock": "<rect x=\"5\" y=\"10\" width=\"14\" height=\"11\" rx=\"2\"/><path d=\"M8 10V6a4 4 0 0 1 8 0v4m-4 5v2\"/>"
};
export function icon(name) {
  const element = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  element.setAttribute('viewBox', '0 0 24 24');
  element.setAttribute('fill', 'none'); element.setAttribute('stroke', 'currentColor');
  element.setAttribute('stroke-width', '1.6'); element.setAttribute('aria-hidden', 'true');
  element.classList.add('icon');
  element.innerHTML = paths[name] || paths.list;
  return element;
}
export function installIcons() { document.querySelectorAll('[data-icon]').forEach(element => element.replaceChildren(icon(element.dataset.icon))); }
