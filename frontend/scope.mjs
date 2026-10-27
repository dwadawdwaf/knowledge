// 知识范围选择：单值（'' 表示全部知识库），只在内存中保存，不发额外请求。
import { el, icon, openMenu, menuItem, closeMenu } from './dom.mjs';

export function scopeIds(app) {
  const { libraries, scope } = app.state;
  if (!libraries.length) return [];
  if (scope && libraries.some(kb => kb.id === scope)) return [scope];
  return libraries.map(kb => kb.id);
}

export function scopeLabel(app) {
  const { libraries, scope } = app.state;
  if (!libraries.length) return '尚无知识库';
  const current = libraries.find(kb => kb.id === scope);
  if (!current) return libraries.length > 1 ? '全部知识库' : libraries[0].name;
  return current.name;
}

export function scopeControl(app, { onChange } = {}) {
  const btn = el('button', 'scope-btn');
  btn.type = 'button';
  btn.setAttribute('aria-haspopup', 'menu');

  const paint = () => {
    btn.replaceChildren();
    const label = el('span', 'scope-label', scopeLabel(app));
    btn.append(icon('database'), label, icon('chevron'));
    btn.title = `检索范围：${scopeLabel(app)}`;
  };
  paint();

  btn.addEventListener('click', () => {
    if (!app.state.libraries.length) {
      app.notify('还没有知识库，先创建一个再提问。');
      return;
    }
    openMenu(btn, menu => {
      menu.append(el('div', 'menu-title', '检索范围'));
      menu.append(menuItem('全部知识库', {
        glyph: 'layers',
        checked: !app.state.scope,
        onClick: () => { app.state.scope = ''; paint(); onChange?.(); },
      }));
      app.state.libraries.forEach(kb => {
        menu.append(menuItem(kb.name, {
          glyph: 'library',
          checked: app.state.scope === kb.id,
          onClick: () => { app.state.scope = kb.id; paint(); onChange?.(); },
        }));
      });
      if (app.state.scope) {
        menu.append(el('div', 'menu-sep'));
        menu.append(menuItem('清除范围限制', { glyph: 'refresh', onClick: () => { closeMenu(); app.state.scope = ''; paint(); onChange?.(); } }));
      }
    }, { align: 'end' });
  });

  return btn;
}
