// 评测结果复用已授权的文档原件接口；页面只读，不从浏览器触发付费批量实验。
import { el, actionButton, emptyState, loading } from './dom.mjs';

export const REPORT_LIBRARY = '检索评测 · 过程与结果';
export const REPORT_FILE = 'retrieval-evaluation-v1.txt';
export const VARIANTS = [['keyword_5', '关键词 @5'], ['semantic_5', '语义 @5'], ['hybrid_5', '混合 @5'], ['hybrid_20', '混合 @20']];
const percent = value => Number.isFinite(value) ? `${Math.round(value * 100)}%` : '—';

export function parseReport(text) {
  if (text.length > 1024 * 1024) throw new Error('评测报告过大，未加载。');
  const value = JSON.parse(text);
  if (value?.schema !== 'retrieval-evaluation-v1' || !['running', 'completed', 'interrupted'].includes(value.status)
      || !Array.isArray(value.cases) || value.cases.length > 24 || !Array.isArray(value.events)
      || !Array.isArray(value.sources) || typeof value.knowledge_base_id !== 'string'
      || !Number.isInteger(value.total_cases) || value.total_cases < 1 || value.total_cases > 24
      || value.cases.some(row => typeof row.id !== 'string' || typeof row.question !== 'string'
        || typeof row.category !== 'string' || typeof row.answerable !== 'boolean' || !row.retrieval || !row.qa
        || VARIANTS.some(([key]) => !row.retrieval[key]))) {
    throw new Error('评测报告格式不兼容，未展示不完整指标。');
  }
  return value;
}

export function needsReview(row) {
  return Boolean(row.qa.error || row.qa.status_matches_label !== true || row.qa.forbidden_detected
    || row.qa.citations_resolvable === false || row.qa.warnings?.length
    || VARIANTS.some(([key]) => row.retrieval[key]?.error || row.retrieval[key]?.degraded)
    || (row.answerable && (row.retrieval.hybrid_5.fact_recall !== 1
      || row.qa.lexical_coverage !== 1 || row.qa.citation_fact_coverage !== 1)));
}

export function renderEvaluation(app) {
  const page = el('div', 'page evaluation-page');
  const head = el('div', 'page-head');
  const copy = el('div');
  copy.append(el('h1', '', '检索评测'), el('p', '', '先观察，再优化。对比召回方式、证据覆盖与问答表现，保留每次实验的真实结果。'));
  const refresh = actionButton('刷新进度', 'btn ghost', () => load(), 'refresh');
  head.append(copy, refresh);
  page.append(head);
  const body = el('div');
  page.append(body);
  let sequence = 0;
  async function load() {
    const current = ++sequence;
    refresh.disabled = true;
    body.replaceChildren(loading('正在读取评测过程与结果…'));
    try {
      if (!app.state.connected) { body.replaceChildren(emptyState('连接后查看评测', '请先连接工作台。')); return; }
      const libraries = await app.api.request('/knowledge-bases');
      if (current !== sequence || !page.isConnected) return;
      const library = libraries.items.find(item => item.name === REPORT_LIBRARY);
      if (!library) {
        body.replaceChildren(emptyState('尚未建立评测', '完成自制资料导入与实验后，过程和结果会显示在这里。手动检索已移至各知识库的「检索」页签。'));
        return;
      }
      const documents = await app.api.request(`/knowledge-bases/${library.id}/documents`);
      const document = documents.items.find(item => item.title === REPORT_FILE && item.current_version_id);
      if (!document) { body.replaceChildren(emptyState('评测准备中', '正在发布首份过程记录，请稍后刷新。')); return; }
      const response = await app.api.raw(`/documents/${document.id}/file`);
      const report = parseReport(await response.text());
      if (current !== sequence || !page.isConnected) return;
      body.replaceChildren(reportView(app, report));
      if (report.status === 'running') setTimeout(() => { if (page.isConnected && current === sequence) load(); }, 15000);
    } catch (error) {
      if (current !== sequence || !page.isConnected) return;
      body.replaceChildren(el('div', 'notice-line warn', error.code ? '评测记录暂不可访问，请刷新或重新连接。' : '评测报告格式不兼容或损坏，请核对生成记录。'));
    } finally { if (current === sequence) refresh.disabled = false; }
  }
  queueMicrotask(load);
  return page;
}

function reportView(app, report) {
  const view = el('div', 'evaluation-content');
  const intro = el('section', 'eval-overview');
  const line = el('div', 'eval-status');
  line.append(el('span', 'chip', { running: '实验进行中', completed: '实验已执行', interrupted: '实验已中断' }[report.status]),
    el('span', 'count-note', `${report.revision} · 更新于 ${new Date(report.updated_at).toLocaleString('zh-CN')}`));
  intro.append(line);
  const metrics = el('div', 'eval-metrics');
  const reviewed = report.cases.filter(needsReview).length;
  for (const [label, value] of [['已完成问题', `${report.cases.length} / ${report.total_cases}`],
    ['评测维度', new Set(report.cases.map(row => row.category)).size], ['请求数 / 上限', `${report.requests} / ${report.request_limit}`],
    ['待复核用例', reviewed]]) {
    const metric = el('div', 'eval-metric');
    metric.append(el('span', '', label), el('strong', '', String(value)));
    metrics.append(metric);
  }
  intro.append(metrics, el('p', 'field-hint', `模型：${report.config?.embedding_model || '未知'} / ${report.config?.generator_model || '未知'}。所有资料与标注均为自制，尚未经业务审定。`));
  intro.append(el('p', 'field-hint', `关键词阈值 ${report.config?.similarity_floor ?? '未知'} · 余弦距离上限 ${report.config?.semantic_max_distance ?? '未知'}。请求上限仅含检索/问答；入库与报告发布另需向量调用，费用未统计。`));
  intro.append(actionButton('查看评测资料', 'btn ghost', () => app.openLibraryById(report.knowledge_base_id), 'library'));
  intro.append(actionButton('在评测资料中提问', 'btn ghost', () => {
    app.state.scope = report.knowledge_base_id;
    app.newSession();
  }, 'chat'));
  intro.append(el('p', 'field-hint', '复测请只选择基础资料库，不要把含标注答案的过程与结果库加入检索范围。'));
  view.append(intro);
  view.append(el('div', 'notice-line warn', '召回覆盖与词面覆盖不等于答案正确率。引用支持仅检查标注事实是否出现在可访问的被引原文中，不代表逐句语义蕴含。无答案题不计算事实召回；接口错误单独计数，不当作正常拒答。'));
  view.append(el('h2', '', '实验对比'));
  const table = el('table', 'eval-table');
  table.innerHTML = '<thead><tr><th>检索方案</th><th>平均事实召回</th><th>MRR</th><th>证据完整用例</th><th>有效 / 可答题</th><th>可答题接口错误</th></tr></thead>';
  const rows = el('tbody');
  VARIANTS.forEach(([key, label]) => {
    const value = report.summary?.[key] || {};
    const row = el('tr');
    [label, percent(value.mean_fact_recall), Number.isFinite(value.mrr) ? value.mrr.toFixed(3) : '—', value.complete_cases ?? '—', `${value.valid_cases ?? 0} / ${value.positive_cases ?? 0}`, value.errors ?? 0]
      .forEach(text => row.append(el('td', '', String(text))));
    rows.append(row);
  });
  table.append(rows);
  const wrap = el('div', 'table-wrap'); wrap.append(table); view.append(wrap);
  view.append(el('h2', '', '分维度概览'));
  const dimensions = el('table', 'eval-table');
  dimensions.innerHTML = '<thead><tr><th>维度</th><th>问题数</th><th>混合 @5 完整覆盖</th><th>回答词面完整</th><th>正常拒答</th><th>问答错误</th></tr></thead>';
  const dimensionRows = el('tbody');
  [...new Set(report.cases.map(row => row.category))].forEach(category => {
    const items = report.cases.filter(row => row.category === category);
    const positive = items.filter(row => row.answerable);
    const negative = items.filter(row => !row.answerable);
    const row = el('tr');
    [category, items.length,
      positive.length ? `${positive.filter(row => row.retrieval.hybrid_5.all_facts_found).length} / ${positive.length}` : '—',
      positive.length ? `${positive.filter(row => row.qa.lexical_coverage === 1).length} / ${positive.length}` : '—',
      negative.length ? `${negative.filter(row => row.qa.status === 'no_evidence').length} / ${negative.length}` : '—',
      items.filter(row => row.qa.error).length].forEach(text => row.append(el('td', '', String(text))));
    dimensionRows.append(row);
  });
  dimensions.append(dimensionRows);
  const dimensionWrap = el('div', 'table-wrap'); dimensionWrap.append(dimensions); view.append(dimensionWrap);
  view.append(el('h2', '', '执行过程'));
  const timeline = el('ol', 'eval-timeline');
  report.events.forEach(event => timeline.append(el('li', '', `${new Date(event.at).toLocaleTimeString('zh-CN')} · ${event.stage} · ${event.count}`)));
  view.append(timeline);
  view.append(el('h2', '', '分维度结果与问题明细'));
  const tools = el('div', 'shoulder');
  const filter = el('select', 'input eval-filter');
  [['all', '全部维度'], ...[...new Set(report.cases.map(row => row.category))].map(name => [name, name])]
    .forEach(([value, label]) => { const option = el('option', '', label); option.value = value; filter.append(option); });
  filter.setAttribute('aria-label', '评测维度');
  const label = el('label', 'eval-check');
  const failures = el('input'); failures.type = 'checkbox'; label.append(failures, document.createTextNode('仅待复核'));
  tools.append(filter, label); view.append(tools);
  const cases = el('div', 'eval-cases');
  const paint = () => {
    cases.replaceChildren();
    report.cases.filter(row => (filter.value === 'all' || row.category === filter.value) && (!failures.checked || needsReview(row)))
      .forEach(row => cases.append(caseView(app, report, row)));
    if (!cases.children.length) cases.append(emptyState('没有匹配用例', '切换维度或关闭待复核筛选。'));
  };
  filter.addEventListener('change', paint); failures.addEventListener('change', paint); paint();
  view.append(cases);
  return view;
}

function caseView(app, report, row) {
  const details = el('details', 'eval-case');
  const summary = el('summary');
  summary.append(el('span', 'chip', row.id), el('strong', '', row.question),
    el('span', needsReview(row) ? 'chip warn' : 'chip', needsReview(row) ? '待复核' : '自动检查符合标注'));
  details.append(summary);
  details.append(el('p', 'field-hint', `${row.category} · ${row.answerable ? '可答题' : '无答案题'} · 问答：${row.qa.error || row.qa.status || '未完成'}`));
  const scores = el('div', 'eval-scores');
  VARIANTS.forEach(([key, label]) => scores.append(el('span', 'chip', `${label} 召回 ${row.retrieval[key].error || percent(row.retrieval[key].fact_recall)}`)));
  scores.append(el('span', 'chip', `回答词面覆盖 ${percent(row.qa.lexical_coverage)}`),
    el('span', 'chip', `引用事实覆盖 ${percent(row.qa.citation_fact_coverage)}`));
  details.append(scores);
  details.append(actionButton('在知识库中复测', 'btn ghost', () => app.openLibrarySearch(report.knowledge_base_id, row.question), 'search'));
  if (row.conversation_id) details.append(actionButton('查看实际问答', 'btn ghost', async () => {
    try {
      await app.loadConversations();
      const session = app.state.sessions.find(item => item.id === row.conversation_id);
      if (session) app.selectSession(session); else app.notify('评测会话已删除或不再可访问。');
    } catch { app.notify('暂时无法读取问答历史，请重新连接后重试。'); }
  }, 'chat'));
  const expected = el('ul', 'eval-expected');
  (row.expected || []).forEach(fact => expected.append(el('li', '', `标注事实：${fact.quote}`)));
  if (expected.children.length) details.append(expected);
  const hits = row.retrieval.hybrid_5.hits || [];
  if (hits.length) {
    details.append(el('p', 'field-label', '本次混合 Top-5 的实际来源'));
    hits.forEach((hit, index) => details.append(actionButton(`${index + 1}. ${hit.title}`, 'link-btn eval-hit', () => app.openSource(hit.chunk_id), 'quote')));
  }
  return details;
}
