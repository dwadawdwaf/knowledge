// 引用原文与分块结果：两个抽屉共用一套读取与渲染逻辑。
import { el, icon, emptyState, loading } from './dom.mjs';
import { chunkMetrics, locatorText, warningLabel, visibleWarnings } from './core.mjs';
import { sheetTable } from './browse.mjs';

export async function openSource(app, chunkId) {
  const dialog = document.querySelector('#source-dialog');
  const content = document.querySelector('#source-content');
  content.replaceChildren(loading('正在读取当前可访问的原文…'));
  dialog.showModal();
  dialog.focus();
  try {
    const chunk = await app.api.request(`/chunks/${chunkId}`);
    content.replaceChildren();
    content.append(el('h3', 'source-title', chunk.title));
    const meta = el('div', 'source-meta');
    meta.append(el('span', 'tag gray', locatorText(chunk.locator)));
    content.append(meta);
    if (chunk.locator?.precision === 'composite') {
      (chunk.locator.sources || []).forEach(source => {
        content.append(el('p', 'source-location', locatorText(source.locator)));
      });
    }
    content.append(el('div', 'source-text', chunk.text || '该片段没有可展示的正文。'));
    // 从引用直接跳到该文件正文并定位到这一段，省去在长文档里翻找。
    const openFile = el('button', 'btn ghost source-open-file');
    openFile.type = 'button';
    openFile.append(icon('folder'), document.createTextNode('在文件中查看'));
    openFile.addEventListener('click', () => {
      dialog.close();
      app.openBrowse({ id: chunk.document_id, title: chunk.title }, { locator: chunk.locator });
    });
    content.append(openFile);
    visibleWarnings(chunk.warnings).forEach(warning => {
      const line = el('div', 'notice-line warn');
      line.style.marginTop = '12px';
      line.append(icon('alert'), el('span', '', warningLabel(warning)));
      content.append(line);
    });
  } catch (error) {
    content.replaceChildren(app.errorBox(error));
  }
}

export async function openChunks(app, doc) {
  const dialog = document.querySelector('#chunks-dialog');
  const content = document.querySelector('#chunks-content');
  content.replaceChildren(loading('正在读取当前可访问的分块…'));
  dialog.showModal();
  dialog.focus();
  try {
    const view = await app.api.request(`/documents/${doc.id}/chunks`);
    const metrics = chunkMetrics(view.items || []);
    content.replaceChildren(summary(view, metrics));
    if (!metrics.count) {
      content.append(emptyState('还没有分块', '文档处理成功后才有可查看的分块结果。'));
      return;
    }
    content.append(map(metrics), list(view.items));
  } catch (error) {
    content.replaceChildren(app.errorBox(error));
  }
}

function summary(view, metrics) {
  const wrap = el('div');
  const head = el('div', 'chunk-heading');
  head.append(el('h3', '', view.title));
  head.append(el('p', '', `${view.version_no ? `第 ${view.version_no} 版` : '尚未发布'} · 构建 ${view.build_id ? view.build_id.slice(0, 8) : '—'}`));
  wrap.append(head);

  const grid = el('div', 'chunk-stats');
  const range = metrics.pages.length
    ? (metrics.pages.length === 1 ? `第 ${metrics.pages[0]} 页` : `第 ${metrics.pages[0]}–${metrics.pages.at(-1)} 页`)
    : '未标注页码';
  [
    ['分块数', view.total, view.truncated ? `已展示前 ${view.items.length} 块` : '全部展示'],
    ['平均字数', metrics.avg, '按已展示分块计算'],
    ['覆盖页码', metrics.pages.length ? metrics.pages.length : '—', range],
    ['重叠字符', metrics.overlaps, metrics.usable ? '相邻分块共享的字符' : '当前定位无字符偏移'],
  ].forEach(([label, value, hint]) => {
    const cell = el('div', 'chunk-stat');
    cell.append(el('span', 'chunk-stat-label', label), el('strong', '', String(value)), el('small', '', hint));
    grid.append(cell);
  });
  wrap.append(grid);
  return wrap;
}

function map(metrics) {
  const box = el('section', 'chunk-map-box');
  box.append(el('h4', '', '分块覆盖'));
  box.append(el('p', 'chunk-map-hint', metrics.usable
    ? (metrics.compound
      ? '按文档字符位置排列。组合分块分别标出每段原文，空白处未被该分块引用。'
      : `按字符区间等比例排列，横向重叠即分块之间的共享内容（文档共 ${metrics.maxEnd} 字符）。`)
    : metrics.blockPrecision
      ? '解析只提供块内偏移，按块序等宽展示，不代表文档字符比例；页码与块序见下方明细。'
      : '当前分块缺少字符偏移，按顺序等宽展示，不代表实际覆盖比例。'));
  const canvas = el('div', 'chunk-map');
  metrics.segments.forEach(segment => {
    const bar = el('span', 'chunk-seg');
    bar.style.left = `${segment.left}%`;
    bar.style.width = `${segment.width}%`;
    bar.title = `#${segment.ordinal + 1} · ${segment.chars} 字${segment.page ? ` · 第 ${segment.page} 页` : ''}`;
    if (segment.pieces?.length) {
      bar.classList.add('chunk-seg-compound');
      for (const piece of segment.pieces) {
        const mark = el('span', 'chunk-source-seg');
        mark.style.left = `${piece.left}%`;
        mark.style.width = `${piece.width}%`;
        bar.append(mark);
      }
    }
    canvas.append(bar);
  });
  box.append(canvas);
  return box;
}

function list(items) {
  const box = el('section', 'chunk-list-box');
  box.append(el('h4', '', '分块明细'));
  items.forEach(item => {
    const card = el('article', 'chunk-card');
    const head = el('div', 'chunk-card-head');
    head.append(el('span', 'chunk-tag', `#${item.ordinal + 1}`));
    head.append(el('span', 'source-location', locatorText(item.locator)));
    head.append(el('span', 'chunk-chars', `${(item.text || '').length} 字`));
    visibleWarnings(item.warnings).forEach(code => {
      head.append(el('span', 'chunk-warn', warningLabel(code)));
    });
    const toggle = el('button', 'chunk-toggle', '展开');
    toggle.type = 'button';
    toggle.setAttribute('aria-expanded', 'false');
    toggle.addEventListener('click', () => {
      const expanded = card.classList.toggle('expanded');
      toggle.textContent = expanded ? '收起' : '展开';
      toggle.setAttribute('aria-expanded', String(expanded));
    });
    head.append(toggle);
    // 表格块与阅读页保持一致：渲染成真表格，而不是一堆制表符
    const sheet = (item.locator || {}).precision === 'sheet'
      ? sheetTable(item.text || '', '')
      : null;
    if (sheet) {
      const scroll = el('div', 'browse-table-wrap');
      scroll.append(sheet.table);
      card.append(head, scroll);
    } else {
      card.append(head, el('p', 'chunk-text', item.text || ''));
    }
    box.append(card);
  });
  return box;
}
