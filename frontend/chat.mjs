// 对话页：800px 居中列、时间分隔、用户气泡右对齐、助手答案无气泡 + 引用芯片。
import { el, icon, toast, autoGrow, clockLabel, dayLabel, loading } from './dom.mjs';
import { warningLabel, visibleWarnings } from './core.mjs';
import { scopeControl, scopeIds, scopeLabel } from './scope.mjs';

const MODE_LABEL = { hybrid: '混合检索', semantic: '语义检索', keyword: '关键词检索' };

const SUGGESTIONS = [
  '这份资料里最关键的规定是什么？',
  '从制度里找出关于流程的条款',
  '帮我把相关章节的原文找出来',
];

export function renderChat(app) {
  const session = app.currentSession();
  const view = el('section', 'chat-view');
  view.append(header(app, session));

  const scroll = el('section', 'chat-scroll');
  scroll.id = 'chat-scroll';
  const column = el('div', 'chat-column');

  const turns = session?.turns || [];
  if (session?.loading) column.append(loading('正在读取对话历史…'));
  else if (!turns.length) column.append(hero(app));

  const thread = el('div', 'thread');
  thread.id = 'thread';
  let previousAt = null;
  turns.forEach(turn => {
    thread.append(turnNode(app, turn, previousAt));
    previousAt = turn.at;
  });
  column.append(thread);
  scroll.append(column);

  view.append(scroll, composer(app));
  if (turns.length) queueMicrotask(scrollToEnd);
  return view;
}

function header(app, session) {
  const head = el('header', 'chat-head');
  const left = el('div', 'chat-head-left');
  left.append(el('h1', 'chat-title', session?.title || '新对话'));

  const right = el('div', 'chat-head-right');
  right.append(iconButtonNewChat(app));
  head.append(left, right);
  return head;
}

function scopeMetaText(app) {
  const ids = scopeIds(app);
  if (!ids.length) return '所选知识库：无';
  if (ids.length > 1) return `所选知识库：全部 ${ids.length} 个`;
  return `所选知识库：${scopeLabel(app)}`;
}

function iconButtonNewChat(app) {
  const btn = el('button', 'icon-btn');
  btn.type = 'button';
  btn.title = '新建对话';
  btn.setAttribute('aria-label', '新建对话');
  btn.append(icon('pen'));
  btn.addEventListener('click', () => app.newSession());
  return btn;
}

function hero(app) {
  const box = el('div', 'chat-hero');
  const mark = el('div', 'hero-mark');
  mark.append(icon('sparkle'));
  box.append(mark);

  if (!app.state.connected) {
    const cta = el('button', 'btn primary', '连接工作台');
    cta.type = 'button';
    cta.addEventListener('click', () => app.openConnection());
    box.append(cta);
    return box;
  }
  if (!app.state.libraries.length) {
    const cta = el('button', 'btn primary', '新建知识库');
    cta.type = 'button';
    cta.addEventListener('click', () => app.createLibrary());
    box.append(cta);
    return box;
  }

  const list = el('div', 'hero-suggestions');
  SUGGESTIONS.forEach(text => {
    const chip = el('button', 'suggestion');
    chip.type = 'button';
    chip.append(icon('quote'), el('span', '', text));
    chip.addEventListener('click', () => {
      const input = document.querySelector('.chat-compose textarea');
      if (!input) return;
      input.value = text;
      input.focus();
      autoGrow(input);
    });
    list.append(chip);
  });
  box.append(list);
  return box;
}

function composer(app) {
  const wrap = el('div', 'composer-wrap');
  const form = el('form', 'chat-compose');
  const card = el('div', 'composer');
  const input = el('textarea');
  input.rows = 1;
  input.maxLength = 2000;
  input.placeholder = '你想从资料中了解什么？';
  input.setAttribute('aria-label', '输入问题');
  input.addEventListener('input', () => autoGrow(input));
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  const bar = el('div', 'composer-bar');
  bar.append(el('span', 'composer-hint', 'Enter 发送 · Shift + Enter 换行'));
  const tools = el('div', 'composer-tools');
  // 检索范围紧挨发送按钮：提问前顺手确认这次依据哪些资料
  tools.append(scopeControl(app));
  const send = el('button', 'send-btn');
  send.type = 'submit';
  send.setAttribute('aria-label', '发送问题');
  send.append(icon('arrow-up'));
  tools.append(send);
  bar.append(tools);

  card.append(input, bar);
  form.append(card);
  wrap.append(form);

  form.addEventListener('submit', event => {
    event.preventDefault();
    const question = input.value.trim();
    if (!question) return;
    if (!app.state.connected) { app.openConnection(); return; }
    if (!scopeIds(app).length) { toast('还没有可用的知识库，先创建并上传资料。'); return; }
    if (scopeIds(app).length > 20) { toast('一次最多检索 20 个知识库，请先选择一个知识库。'); return; }
    if (app.state.busy) { toast('上一个问题还在处理中，请稍候。'); return; }
    input.value = '';
    autoGrow(input);
    ask(app, question);
  });

  setTimeout(() => { if (input.isConnected && document.activeElement?.tagName !== 'TEXTAREA') input.focus({ preventScroll: true }); }, 0);
  return wrap;
}

async function ask(app, question) {
  const thread = document.querySelector('#thread');
  if (!thread) return;
  document.querySelector('.chat-hero')?.remove();

  const session = app.currentSession() || app.ensureSession();
  const ids = [...scopeIds(app)];
  const revision = app.api.revision;
  const turn = { id: uid(), question, at: new Date(), status: 'pending' };
  session.turns.push(turn);
  if (!session.title) session.title = question.slice(0, 40);
  app.renderSessions();

  const node = turnNode(app, turn, session.turns.at(-2)?.at || null);
  thread.append(node);
  scrollToEnd();
  app.state.busy = true;
  // 提问这一刻的知识范围：之后切换范围不应该改写历史消息上的标注
  turn.scope = scopeMetaText(app);

  try {
    await app.ensureConversation(session, question);
    app.renderSessions();
    const answer = await app.api.request('/qa', {
      method: 'POST',
      // 本地部署的生成模型单次可能跑到 40–90 秒，留足与后端 KS_GENERATOR_TIMEOUT_SECONDS 对齐的余量。
      timeout: 180000,
      body: {
        question,
        knowledge_base_ids: ids,
        conversation_id: session.id,
      },
    });
    turn.status = 'done';
    turn.answer = answer;
    session.loaded = true;
  } catch (error) {
    turn.status = 'error';
    turn.error = error;
  } finally {
    if (revision === app.api.revision) {
      app.state.busy = false;
      if (app.state.page === 'chat' && app.currentSession() === session) {
        const target = [...document.querySelectorAll('#thread .turn')].find(item => item.dataset.turnId === turn.id);
        target?.replaceWith(turnNode(app, turn, session.turns.at(-2)?.at || null));
        const title = document.querySelector('.chat-title');
        if (title) title.textContent = session.title || '新对话';
        scrollToEnd();
      }
      app.renderSessions();
      app.flushUpdates();
    }
  }
}

function scrollToEnd() {
  const scroll = document.querySelector('#chat-scroll');
  if (!scroll) return;
  scroll.scrollTop = scroll.scrollHeight;
}

function turnNode(app, turn, previousAt) {
  const wrap = el('article', 'turn');
  wrap.dataset.turnId = turn.id;
  const gap = previousAt ? turn.at - previousAt : null;
  if (!previousAt || gap > 10 * 60 * 1000) {
    wrap.append(el('div', 'turn-index', `${dayLabel(turn.at)} · ${clockLabel(turn.at)}`));
  }
  const row = el('div', 'user-row');
  row.append(el('div', 'user-bubble', turn.question));
  wrap.append(row);

  const answer = el('div', 'answer-message');
  if (turn.status === 'pending') {
    const thinking = el('div', 'thinking');
    thinking.append(el('span', 'spinner'), el('span', '', '正在检索证据并生成回答…'));
    answer.append(headBlock('正在检索'), thinking);
  } else if (turn.status === 'error') {
    answer.append(headBlock('请求未完成'), app.errorBox(turn.error));
  } else {
    answer.append(...answerBlocks(app, turn.answer, turn.scope));
  }
  wrap.append(answer);
  return wrap;
}

function headBlock(title, extra) {
  const head = el('div', 'answer-head');
  const avatar = el('span', 'answer-avatar');
  avatar.append(icon('sparkle'));
  head.append(avatar, el('strong', '', title));
  if (extra) head.append(extra);
  return head;
}

function answerBlocks(app, answer, scope) {
  const noEvidence = answer.answer_status === 'no_evidence';
  const conflict = answer.answer_status === 'conflicting_evidence';
  const blocks = [];

  const meta = el('span', 'meta');
  const parts = [];
  if (scope) parts.push(scope);
  const hits = answer.retrieval?.hits;
  if (typeof hits === 'number' && hits > 0) parts.push(`命中 ${hits} 个片段`);
  if (answer.retrieval?.mode) parts.push(MODE_LABEL[answer.retrieval.mode] || answer.retrieval.mode);
  if (parts.length) {
    meta.textContent = ` · ${parts.join(' · ')}`;
    blocks.push(headBlock(noEvidence ? '未找到回答依据' : conflict ? '证据存在冲突' : '知识库回答', meta));
  } else {
    blocks.push(headBlock(noEvidence ? '未找到回答依据' : conflict ? '证据存在冲突' : '知识库回答'));
  }

  const text = (answer.answer || '').trim();
  const fallback = noEvidence ? '所选资料中没有足够依据，未生成回答。可以扩大知识范围，或换一种问法。' : '';
  const body = text || fallback;
  if (body) blocks.push(el('div', noEvidence ? 'answer-body none' : 'answer-body', body));

  const citations = answer.citations || [];
  if (citations.length) {
    const foot = el('div', 'answer-foot');
    foot.append(el('div', 'citations-label', '引用原文 · 点击查看片段'));
    const chips = el('div', 'citations');
    citations.forEach(cite => {
      const chip = el('button', 'citation-chip');
      chip.type = 'button';
      chip.title = cite.excerpt || cite.title;
      chip.append(el('b', '', String(cite.index)), el('span', '', cite.title || '未命名文档'));
      chip.addEventListener('click', () => app.openSource(cite.chunk_id));
      chips.append(chip);
    });
    foot.append(chips);
    blocks.push(foot);
  }

  visibleWarnings(answer.warnings).forEach(warning => {
    const line = el('div', 'notice-line warn');
    line.append(icon('alert'), el('span', '', warningLabel(warning)));
    blocks.push(line);
  });
  if (answer.retrieval?.degraded) {
    const line = el('div', 'notice-line warn');
    line.append(icon('alert'), el('span', '', '本次检索发生降级，结果可能不完整。'));
    blocks.push(line);
  }
  return blocks;
}

function uid() {
  if (globalThis.crypto?.randomUUID) return crypto.randomUUID();
  return `t-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}
