// 视图原语：只做 DOM 与展示转换，不含接口调用，也不引入任何运行时依赖。

const NS = 'http://www.w3.org/2000/svg';

const PATHS = {
  chat: '<path d="M20 14a2 2 0 0 1-2 2H8l-4 4V6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2z"/>',
  library: '<rect x="3" y="4" width="7" height="16" rx="1.5"/><rect x="14" y="4" width="7" height="10" rx="1.5"/><path d="M14 18h7"/>',
  folder: '<path d="M3 7.5A2.5 2.5 0 0 1 5.5 5h3.2a2 2 0 0 1 1.6.8l1 1.3H19a2 2 0 0 1 2 2v7.4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m16.5 16.5 4 4"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  pen: '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/>',
  panel: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  chevron: '<path d="m6 9 6 6 6-6"/>',
  'arrow-up': '<path d="M12 19V5m-6 6 6-6 6 6"/>',
  'arrow-left': '<path d="M19 12H5m6 6-6-6 6-6"/>',
  'arrow-right': '<path d="M5 12h14m-6-6 6 6-6 6"/>',
  refresh: '<path d="M20 11a8 8 0 1 0-.7 4.3"/><path d="M20 4v7h-7"/>',
  upload: '<path d="M12 16V4m-4 4 4-4 4 4"/><path d="M4 16v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/>',
  file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
  trash: '<path d="M4 7h16M10 7V4h4v3M6 7l1 13h10l1-13"/>',
  sparkle: '<path d="m12 3 2 5 5 2-5 2-2 5-2-5-5-2 5-2z"/><path d="m19 14.5.9 2.1 2.1.9-2.1.9-.9 2.1-.9-2.1-2.1-.9 2.1-.9z"/>',
  shield: '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6z"/><path d="m9 12 2 2 4-4"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M12 2.5v3M12 18.5v3M4.2 6.8l2.1 2.1M17.7 15.1l2.1 2.1M2.5 12h3M18.5 12h3M4.2 17.2l2.1-2.1M17.7 8.9l2.1-2.1"/>',
  moon: '<path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6l1.4 1.4M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4"/>',
  menu: '<path d="M3 6h18M3 12h18M3 18h18"/>',
  more: '<circle cx="5" cy="12" r="1.2"/><circle cx="12" cy="12" r="1.2"/><circle cx="19" cy="12" r="1.2"/>',
  check: '<path d="m5 13 4 4L19 7"/>',
  stop: '<rect x="7.5" y="7.5" width="9" height="9" rx="2"/>',
  book: '<path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H18v18H6.5A2.5 2.5 0 0 1 4 18.5z"/><path d="M8 3v18"/>',
  layers: '<path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 1.8"/>',
  alert: '<path d="M12 4.5 21 20H3z"/><path d="M12 10v4M12 17v.5"/>',
  info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5M12 8v.5"/>',
  external: '<path d="M14 4h6v6"/><path d="M20 4 10 14"/><path d="M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4"/>',
  database: '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  quote: '<path d="M8 7H5a2 2 0 0 0-2 2v3h4z"/><path d="M18 7h-3a2 2 0 0 0-2 2v3h4z"/><path d="M8 12v2a3 3 0 0 1-3 3M18 12v2a3 3 0 0 1-3 3"/>',
};

export function icon(name, size) {
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.7');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  if (size) { svg.setAttribute('width', String(size)); svg.setAttribute('height', String(size)); }
  // 仅固定图标模板使用 innerHTML；用户内容一律以文本节点渲染。
  svg.innerHTML = PATHS[name] || PATHS.file;
  return svg;
}

export function el(tag, className = '', text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

export function hydrateIcons(root = document) {
  root.querySelectorAll('[data-icon]').forEach(node => {
    node.append(icon(node.dataset.icon));
    node.removeAttribute('data-icon');
  });
}

export function iconButton(name, label, onClick, className = 'icon-btn') {
  const node = el('button', className);
  node.type = 'button';
  node.setAttribute('aria-label', label);
  node.title = label;
  node.append(icon(name));
  if (onClick) node.addEventListener('click', onClick);
  return node;
}

export function actionButton(label, className, onClick, glyph) {
  const node = el('button', className);
  node.type = 'button';
  if (glyph) node.append(icon(glyph));
  node.append(document.createTextNode(label));
  if (onClick) node.addEventListener('click', onClick);
  return node;
}

export function loading(text = '正在加载…') {
  const box = el('div', 'loading');
  box.append(el('span', 'spinner'), el('span', '', text));
  return box;
}

export function emptyState(title, description, action) {
  const node = el('div', 'empty');
  const mark = el('div', 'empty-mark');
  mark.append(icon('layers'));
  node.append(mark, el('h3', '', title));
  if (description) node.append(el('p', '', description));
  if (action) node.append(action);
  return node;
}

export function noticeLine(text, kind = '') {
  const node = el('div', kind === 'warn' ? 'notice-line warn' : 'notice-line');
  node.append(icon(kind === 'warn' ? 'alert' : 'info'), el('span', '', text));
  return node;
}

/* 轻提示：单例元素，只切换隐藏状态，不做动画库依赖。 */
let noticeTimer;
export function toast(message, duration = 3600) {
  const node = document.querySelector('#notice');
  if (!node) return;
  node.textContent = message;
  node.hidden = false;
  // 先移除再添加，保证连续提示也能重新触发过渡。
  node.classList.remove('show');
  requestAnimationFrame(() => node.classList.add('show'));
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => {
    node.classList.remove('show');
    noticeTimer = setTimeout(() => { node.hidden = true; }, 200);
  }, duration);
}

/* 浮层菜单：点击锚点展开，点击外部或 Esc 关闭，随窗口变化关闭。 */
let openMenuNode = null;
let menuTimer;
let menuAnchor = null;

export function closeMenu() {
  clearTimeout(menuTimer);
  if (!openMenuNode) return;
  openMenuNode.remove();
  openMenuNode = null;
  menuAnchor?.setAttribute('aria-expanded', 'false');
  menuAnchor = null;
  document.removeEventListener('mousedown', onMenuPointer, true);
  document.removeEventListener('keydown', onMenuKey, true);
  window.removeEventListener('resize', closeMenu);
  window.removeEventListener('scroll', closeMenu, true);
}

function onMenuPointer(event) {
  if (openMenuNode && !openMenuNode.contains(event.target)) closeMenu();
}

function onMenuKey(event) {
  if (event.key === 'Escape') {
    const anchor = menuAnchor;
    closeMenu();
    anchor?.focus();
  }
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    const items = [...openMenuNode.querySelectorAll('button')];
    const current = items.indexOf(document.activeElement);
    const step = event.key === 'ArrowDown' ? 1 : -1;
    event.preventDefault();
    items[(current + step + items.length) % items.length]?.focus();
  }
}

export function openMenu(anchor, build, { align = 'start' } = {}) {
  closeMenu();
  const menu = el('div', 'menu');
  menu.setAttribute('role', 'menu');
  build(menu);
  document.body.append(menu);

  const rect = anchor.getBoundingClientRect();
  const width = menu.offsetWidth;
  const height = menu.offsetHeight;
  let left = align === 'end' ? rect.right - width : rect.left;
  left = Math.max(8, Math.min(left, window.innerWidth - width - 8));
  let top = rect.bottom + 6;
  if (top + height > window.innerHeight - 8) top = Math.max(8, rect.top - height - 6);
  menu.style.left = `${Math.round(left)}px`;
  menu.style.top = `${Math.round(top)}px`;

  openMenuNode = menu;
  menuAnchor = anchor;
  anchor.setAttribute('aria-expanded', 'true');
  menuTimer = setTimeout(() => {
    if (openMenuNode !== menu) return;
    document.addEventListener('mousedown', onMenuPointer, true);
    document.addEventListener('keydown', onMenuKey, true);
    window.addEventListener('resize', closeMenu);
    window.addEventListener('scroll', closeMenu, true);
  }, 0);
  return menu;
}

export function menuItem(label, { onClick, glyph, danger = false, checked = false } = {}) {
  const node = el('button', danger ? 'menu-item danger' : 'menu-item');
  node.type = 'button';
  node.setAttribute('role', 'menuitem');
  if (glyph) node.append(icon(glyph));
  node.append(el('span', '', label));
  if (checked) {
    const mark = el('span', 'check');
    mark.append(icon('check'));
    node.append(mark);
  }
  node.addEventListener('click', () => {
    closeMenu();
    if (onClick) onClick();
  });
  return node;
}

/* 时间展示：创建时间用相对描述，交互时间用本地时:分。 */
export function fmtDate(iso) {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return `${date.getFullYear()}/${String(date.getMonth() + 1).padStart(2, '0')}/${String(date.getDate()).padStart(2, '0')}`;
}

export function relativeCreated(iso) {
  if (!iso) return '创建时间未知';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '创建时间未知';
  const start = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const today = new Date();
  const todayStart = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  const days = Math.round((todayStart - start) / 86400000);
  if (days <= 0) return '今天创建';
  if (days === 1) return '昨天创建';
  if (days < 7) return `${days} 天前创建`;
  if (days < 30) return `${Math.floor(days / 7)} 周前创建`;
  if (days < 365) return `${Math.floor(days / 30)} 个月前创建`;
  return `${Math.floor(days / 365)} 年前创建`;
}

export function clockLabel(date = new Date()) {
  return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
}

export function dayLabel(date = new Date()) {
  return `${date.getFullYear()} 年 ${date.getMonth() + 1} 月 ${date.getDate()} 日`;
}

/* 文本域自适应高度：上限 190px，超出后内部滚动。 */
export function autoGrow(textarea, max = 190) {
  if (!textarea) return;
  textarea.style.height = 'auto';
  textarea.style.height = `${Math.min(textarea.scrollHeight, max)}px`;
}
