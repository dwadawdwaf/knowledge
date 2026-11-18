// 对话页：800px 居中列、时间分隔、用户气泡右对齐、助手答案无气泡 + 引用芯片。
// 提问走 SSE：先显示真实执行阶段，再边收边渲染正文，最后用校验后的答案覆盖。
// 每一轮回答都保留自己的执行过程（可展开/折叠）；发送与停止共用输入框右下角这一个按钮。
import { el, icon, toast, autoGrow, clockLabel, dayLabel, loading, noticeLine } from './dom.mjs';
import { warningLabel, visibleWarnings } from './core.mjs';
import { scopeControl, scopeIds, scopeLabel } from './scope.mjs';
import { streamAnswer, sendButtonView, EXECUTION_STATE, durationText } from './stream.mjs';

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

/* -------------------------------------------------------- 发送 / 停止 */

// 正在流式返回本轮回答。与 app.state.busy 分开：busy 还覆盖上传和检索。
let composerStreaming = false;
let composerButton = null;

function applySendButton(button, streaming) {
  if (!button) return;
  const view = sendButtonView(streaming);
  button.className = view.className;
  button.setAttribute('aria-label', view.label);
  button.title = streaming ? view.label : '';
  button.replaceChildren(icon(view.icon));
}

function setStreaming(streaming) {
  composerStreaming = streaming;
  // 视图可能被重建过，按钮引用失效时按 class 重新取一次。
  if (!composerButton?.isConnected) {
    composerButton = document.querySelector('.chat-compose .send-btn') || composerButton;
  }
  applySendButton(composerButton, streaming);
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
  // 生成中同一个按钮就是停止按钮：拦下提交，改为中断当前流。
  send.addEventListener('click', event => {
    if (!composerStreaming) return;
    event.preventDefault();
    cancelActiveRun();
  });
  composerButton = send;
  applySendButton(send, composerStreaming);
  tools.append(send);
  bar.append(tools);

  card.append(input, bar);
  form.append(card);
  wrap.append(form);

  form.addEventListener('submit', event => {
    event.preventDefault();
    // 兜底：按钮点击未拦住提交时，这里也不允许并发第二个请求。
    if (composerStreaming) { cancelActiveRun(); return; }
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

  const controller = new AbortController();
  activeRun = { controller, turnId: turn.id };
  setStreaming(true);
  const live = new LiveAnswer(app, node);
  const sameSession = () => revision === app.api.revision && app.currentSession() === session;

  try {
    await app.ensureConversation(session, question);
    app.renderSessions();
    live.attach();
    const run = await streamAnswer(
      app.api,
      { question, knowledge_base_ids: ids, conversation_id: session.id },
      {
        signal: controller.signal,
        // 用回调参数里的 run：此时外层的 run 还没赋值（await 尚未返回）。
        // 同时把阶段同步到 turn：流式中途切换页面再回来仍能看到当前进度。
        onEvent: (change, _kind, _data, state) => {
          turn.stages = state.stages;
          if (!sameSession()) return;
          live.update(state, change);
        },
      },
    );
    session.loaded = true;
    // 客户端观测的整轮耗时，与状态一起存进本轮，重绘和折叠后仍然显示。
    turn.durationMs = run.startedAt && run.endedAt ? run.endedAt - run.startedAt : 0;
    // 三种收尾分开记：停止与失败都不能写成 completed，停止还要保留已到达的正文。
    if (run.status === 'stopped') {
      turn.status = 'stopped';
      turn.text = run.text;
      turn.stages = run.stages;
    } else if (run.error) {
      turn.status = 'error';
      turn.error = run.error;
      turn.stages = run.stages;
    } else {
      turn.status = 'done';
      turn.answer = run.answer;
      turn.stages = run.stages;
    }
  } catch (error) {
    turn.status = 'error';
    turn.error = error;
  } finally {
    activeRun = null;
    setStreaming(false);
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

/** 当前流式轮次；切换会话或新建对话时用于中断，避免把答案写进别的会话。 */
let activeRun = null;

export function cancelActiveRun() {
  activeRun?.controller.abort();
  activeRun = null;
}

/**
 * 一轮回答的执行过程。同一份结构贯穿生成中、完成、停止与失败：
 * 生成中默认展开，结束后由调用方决定折叠，但阶段数据（stages）永不丢弃。
 */
class ExecutionPanel {
  constructor({ status = 'running', stages = [], expanded = false, durationMs = 0 } = {}) {
    this.status = status;
    this.stages = stages;
    this.expanded = expanded;
    this.durationMs = durationMs;

    this.root = el('div', 'exec');
    this.toggle = el('button', 'exec-toggle');
    this.toggle.type = 'button';
    this.mark = el('span', 'exec-mark');
    this.title = el('span', 'exec-title');
    this.hint = el('span', 'exec-hint');
    this.chevron = el('span', 'exec-chevron');
    this.chevron.append(icon('chevron'));
    this.toggle.append(this.mark, this.title, this.hint, this.chevron);
    this.list = el('div', 'stages');
    this.root.append(this.toggle, this.list);
    this.toggle.addEventListener('click', () => this.setExpanded(!this.expanded));
    this.render();
  }

  setStages(stages) {
    this.stages = stages || [];
    this.render();
  }

  setStatus(status, durationMs = this.durationMs) {
    this.status = status;
    this.durationMs = durationMs;
    this.render();
  }

  setExpanded(expanded) {
    this.expanded = expanded;
    this.render();
  }

  render() {
    const meta = EXECUTION_STATE[this.status] || EXECUTION_STATE.running;
    const spent = this.status === 'running' ? '' : durationText(this.durationMs);
    this.root.dataset.status = this.status;
    this.mark.replaceChildren(icon(meta.icon));
    this.title.textContent = spent ? `${meta.label}（${spent}）` : meta.label;
    this.hint.textContent = this.expanded ? '收起' : '查看执行过程';
    this.toggle.setAttribute('aria-expanded', String(this.expanded));
    this.list.hidden = !this.expanded;
    this.list.replaceChildren(...this.stages.map(stageRow));
  }
}

/** 一轮的执行过程：放在正文上方，结束后默认折叠，但保留可点击入口，阶段不随重绘丢失。 */
function executionBlock(stages, status, { expanded = false, durationMs = 0 } = {}) {
  if (!stages?.length) return null;
  return new ExecutionPanel({ status, stages, expanded, durationMs }).root;
}

/**
 * 流式期间就地更新单个回答节点：阶段列表随事件推进，正文逐块追加，
 * 引用校验通过后挂上芯片。完成、停止或失败时由 `turnNode` 重新渲染整轮。
 */
class LiveAnswer {
  constructor(app, turnNodeEl) {
    this.app = app;
    this.answer = turnNodeEl.querySelector('.answer-message');
    this.panel = new ExecutionPanel({ status: 'running', expanded: true });
    this.body = el('div', 'answer-body streaming');
    // 首块正文到达前不占位：检索阶段只显示阶段列表。
    this.body.hidden = true;
    this.foot = el('div', 'answer-foot');
    this.text = '';
    this.frame = 0;
  }

  attach() {
    if (!this.answer) return;
    this.answer.replaceChildren(headBlock('正在回答'), this.panel.root, this.body, this.foot);
  }

  update(run, change) {
    if (!this.answer) return;
    if (['stage', 'error', 'final', 'stopped'].includes(change)) {
      this.panel.setStages(run.stages);
      this.panel.setStatus(
        run.status,
        run.startedAt && run.endedAt ? run.endedAt - run.startedAt : 0,
      );
    }
    if (change === 'delta') {
      this.text = run.text;
      this.scheduleText();
    }
    if (change === 'stage' && run.citations.length && !this.foot.childElementCount) {
      this.renderCitations(run.citations);
    }
  }

  renderCitations(citations) {
    this.foot.append(el('div', 'citations-label', '引用原文 · 点击查看片段'));
    const chips = el('div', 'citations');
    citations.forEach(cite => {
      const chip = el('button', 'citation-chip');
      chip.type = 'button';
      chip.title = cite.excerpt || cite.title;
      chip.append(el('b', '', String(cite.index)), el('span', '', cite.title || '未命名文档'));
      chip.addEventListener('click', () => this.app.openSource(cite.chunk_id));
      chips.append(chip);
    });
    this.foot.append(chips);
  }

  scheduleText() {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = 0;
      if (!this.body) return;
      this.body.hidden = !this.text;
      this.body.textContent = this.text;
      scrollToEnd();
    });
  }
}

function stageRow(stage) {
  const row = el('div', `stage ${stage.state}`);
  const mark = el('span', 'stage-mark');
  if (stage.state === 'active') mark.append(el('span', 'spinner'));
  else if (stage.state === 'failed') mark.append(icon('alert'));
  else if (stage.state === 'stopped') mark.append(el('span', 'stage-dot'));
  else mark.append(icon('check'));
  row.append(mark, el('span', 'stage-label', stage.label));
  if (stage.detail) row.append(el('span', 'stage-detail', stage.detail));
  return row;
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
    // 流式开始前由 LiveAnswer 就地接管；重绘时若已有阶段，直接按展开状态还原。
    const panel = executionBlock(turn.stages, 'running', { expanded: true });
    answer.append(headBlock('正在回答'), ...(panel ? [panel] : [thinkingBlock()]));
  } else if (turn.status === 'stopped') {
    answer.append(...stoppedBlocks(turn));
  } else if (turn.status === 'error') {
    const panel = executionBlock(turn.stages, 'error', { durationMs: turn.durationMs });
    answer.append(headBlock('执行失败'), ...(panel ? [panel] : []), app.errorBox(turn.error));
  } else {
    answer.append(...answerBlocks(app, turn.answer, turn.scope, turn));
  }
  wrap.append(answer);
  return wrap;
}

function thinkingBlock(text = '正在准备回答…') {
  const thinking = el('div', 'thinking');
  thinking.append(el('span', 'spinner'), el('span', '', text));
  return thinking;
}

/** 用户主动停止：保留已到达的正文，但明确标注它没有通过引用校验、也没有保存。 */
function stoppedBlocks(turn) {
  const blocks = [headBlock('已停止')];
  const panel = executionBlock(turn.stages, 'stopped', { durationMs: turn.durationMs });
  if (panel) blocks.push(panel);
  blocks.push(noticeLine('已停止生成。本轮回答未完成校验，不会保存到会话。'));
  const text = (turn.text || '').trim();
  if (text) blocks.push(el('div', 'answer-body paused', text));
  return blocks;
}

function headBlock(title, extra) {
  const head = el('div', 'answer-head');
  const avatar = el('span', 'answer-avatar');
  avatar.append(icon('sparkle'));
  head.append(avatar, el('strong', '', title));
  if (extra) head.append(extra);
  return head;
}

function answerBlocks(app, answer, scope, turn = null) {
  const noEvidence = answer.answer_status === 'no_evidence';
  const conflict = answer.answer_status === 'conflicting_evidence';
  const blocks = [];

  const meta = el('span', 'meta');
  const parts = [];
  if (scope) parts.push(scope);
  const hits = answer.retrieval?.hits;
  if (typeof hits === 'number' && hits > 0) parts.push(`命中 ${hits} 个片段`);
  if (answer.retrieval?.mode) parts.push(MODE_LABEL[answer.retrieval.mode] || answer.retrieval.mode);
  const used = answer.retrieval?.context_messages;
  if (typeof used === 'number' && used > 0) parts.push(`结合 ${used} 条历史`);
  if (parts.length) {
    meta.textContent = ` · ${parts.join(' · ')}`;
    blocks.push(headBlock(noEvidence ? '未找到回答依据' : conflict ? '证据存在冲突' : '知识库回答', meta));
  } else {
    blocks.push(headBlock(noEvidence ? '未找到回答依据' : conflict ? '证据存在冲突' : '知识库回答'));
  }

  // 参考 DeepSeek：执行过程在正文上方，折叠成一行，点开可看完整阶段与耗时。
  const panel = turn
    ? executionBlock(turn.stages, 'completed', { durationMs: turn.durationMs })
    : null;
  if (panel) blocks.push(panel);

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
