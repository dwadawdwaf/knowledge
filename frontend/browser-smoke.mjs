// 使用独立浏览器配置和原生 CDP 验证真实页面；不读取个人浏览器状态或新增 npm 依赖。
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const output = path.join(root, 'backend', '.data', 'workbench', randomUUID().slice(0, 8));
await mkdir(output, { recursive: true });
const executable = process.env.KS_UI_BROWSER;
assert.ok(executable, '必须显式提供测试浏览器路径');
const browser = spawn(executable, ['--headless=new', '--remote-debugging-port=0', `--user-data-dir=${path.join(output, 'profile')}`,
  '--no-first-run', '--no-default-browser-check', '--disable-background-networking', '--disable-sync', '--disable-component-update',
  '--no-proxy-server', '--window-size=1440,1000', '--force-device-scale-factor=1', process.env.KS_UI_URL], { stdio: 'ignore', windowsHide: true });
let socket;
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(read, message, timeout = 30000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) { try { const result = await read(); if (result) return result; } catch {} await delay(150); }
  throw new Error(message);
}
try {
  const lines = await until(async () => (await readFile(path.join(output, 'profile', 'DevToolsActivePort'), 'utf8')).trim().split('\n'), '浏览器调试端口未就绪');
  const targets = await (await fetch(`http://127.0.0.1:${lines[0]}/json/list`)).json();
  const target = targets.find(item => item.type === 'page'); assert.ok(target);
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener('open', resolve); socket.addEventListener('error', reject); });
  let nextId = 0; const pending = new Map();
  socket.addEventListener('message', event => { const message = JSON.parse(event.data); if (!message.id) return; const item = pending.get(message.id); if (!item) return; pending.delete(message.id); message.error ? item.reject(new Error(message.error.message)) : item.resolve(message.result); });
  function send(method, params = {}) { return new Promise((resolve, reject) => { const id = ++nextId; pending.set(id, { resolve, reject }); socket.send(JSON.stringify({ id, method, params })); }); }
  async function js(expression) { const result = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true }); if (result.exceptionDetails) throw new Error('页面执行异常'); return result.result.value; }
  async function wait(expression, timeout = 60000) { return until(() => js(expression), '页面没有进入预期状态', timeout); }
  async function shot(name) { const image = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false }); await writeFile(path.join(output, name + '.png'), Buffer.from(image.data, 'base64')); }
  const clickText = text => js(`[...document.querySelectorAll('button')].find(n=>n.textContent.trim()===${JSON.stringify(text)}).click()`);
  async function setFile(selector, file) { const dom = await send('DOM.getDocument'); const node = await send('DOM.querySelector', { nodeId: dom.root.nodeId, selector }); assert.ok(node.nodeId, '文件输入框未找到'); await send('DOM.setFileInputFiles', { nodeId: node.nodeId, files: [file] }); }
  const rowButton = (file, label) => js(`(()=>{const row=[...document.querySelectorAll('tbody tr')].find(r=>r.textContent.includes(${JSON.stringify(file)}));const target=[...row.querySelectorAll('button')].find(n=>n.textContent.trim()===${JSON.stringify(label)});target.click();return true})()`);
  await send('Page.enable'); await send('Runtime.enable');
  console.log('[索引提示] 注入历史索引缺口，验证工作台可连接、状态检查失败不阻断、恢复后提示消失');
  let indexStatusMode = 'missing';
  const indexStatusInterceptor = async event => {
    const message = JSON.parse(event.data);
    if (message.method !== 'Fetch.requestPaused') return;
    const failed = indexStatusMode === 'failed';
    const payload = failed
      ? { error: { code: 'SERVICE_UNAVAILABLE' } }
      : { data: { status: indexStatusMode === 'missing' ? 'attention' : 'ready', missing_keyword_chunks: indexStatusMode === 'missing' ? 134 : 0, legacy_pending_jobs: 0 } };
    await send('Fetch.fulfillRequest', {
      requestId: message.params.requestId, responseCode: failed ? 503 : 200,
      responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
      body: Buffer.from(JSON.stringify(payload)).toString('base64'),
    });
  };
  socket.addEventListener('message', indexStatusInterceptor);
  await send('Fetch.enable', { patterns: [{ urlPattern: '*/api/v1/search/status', requestStage: 'Request' }] });
  const statusTestUrl = new URL(process.env.KS_UI_URL);
  statusTestUrl.searchParams.set('index-warning-test', randomUUID());
  await send('Page.navigate', { url: statusTestUrl.toString() });
  await wait(`document.querySelector('#app').dataset.connected === 'true' && document.querySelector('#retrieval-notice')?.textContent.includes('134')`);
  await shot('index-warning');
  indexStatusMode = 'failed';
  await js(`document.querySelector('#retrieval-notice button').click()`);
  await wait(`document.querySelector('#retrieval-notice')?.textContent.includes('暂时无法检查')`);
  assert.equal(await js(`document.querySelector('#app').dataset.connected`), 'true');
  indexStatusMode = 'ready';
  await js(`document.querySelector('#retrieval-notice button').click()`);
  await wait(`!document.querySelector('#retrieval-notice')`);
  await send('Fetch.disable'); socket.removeEventListener('message', indexStatusInterceptor);
  await wait(`document.querySelector('#content .primary')`);
  console.log('[浏览器 1/9] 打开带令牌的地址即自动连接，地址栏随即清除令牌');
  await wait(`document.querySelector('#app').dataset.connected === 'true'`);
  assert.equal(await js(`document.querySelector('#connection-dialog').open`), false, '自动连接后不应再弹连接窗口');
  assert.equal(await js(`location.hash.includes('t=')`), false, '令牌必须从地址栏清除');
  assert.equal(await js(`localStorage.length + sessionStorage.length`), 0);
  assert.ok(await js(`document.querySelectorAll('.kb-card').length >= 3`));
  // 库是持久化的：用相对数量断言，重复跑不会因为历史数据而失败
  const kbBefore = await js(`document.querySelectorAll('.kb-card').length`);
  await clickText('新建知识库'); await js(`document.querySelector('#action-input').value='浏览器创建 · 自制样本'; document.querySelector('#action-form').requestSubmit()`);
  await wait(`!document.querySelector('#action-dialog').open && document.querySelectorAll('.kb-card').length===${kbBefore + 1}`);
  await wait(`document.querySelector('#notice').hidden`); await shot('knowledge');
  console.log('[浏览器 2/9] 打开知识库，通过文件输入上传实际自制文档');
  await clickText('行政制度 · 自制样本'); await wait(`document.querySelector('.document-table')`);
  assert.equal(await js(`document.querySelectorAll('.parser-choice option').length`), 3);
  assert.equal(await js(`getComputedStyle(document.querySelector('.parser-choice')).height`), '38px');
  // 用纯文本验证显式选择的请求/冻结，不要求本机实际安装MinerU。
  await js(`document.querySelector('.parser-choice').value='mineru-api4.1-v1'`);
  assert.equal(await js(`[...document.querySelectorAll('.shoulder button')].some(n => n.textContent.trim() === '刷新')`), true);
  // 上传入口只保留上传区一处，工具条不再重复放「上传文档」。
  assert.equal(await js(`[...document.querySelectorAll('.shoulder button')].some(n => n.textContent.trim() === '上传文档')`), false);
  assert.equal(await js(`[...document.querySelectorAll('.upload-actions button')].map(n => n.textContent.trim()).join('|')`), '上传文档');
  await setFile('.upload-zone input[type=file]', path.join(root, 'backend/testdata/engineering/expense.md'));
  await wait(`!![...document.querySelectorAll('tbody tr')].find(row=>row.textContent.includes('expense.md') && row.textContent.includes('已就绪'))`, 120000);
  assert.ok(await js(`document.querySelector('#upload-feedback').textContent.includes('MinerU')`));
  console.log('[浏览器 3/9] 打开分块结果，核对覆盖条与明细一一对应');
  // 分块夹具也含会议室章节，单独放在项目库，避免影响后续行政库的引用验收。
  await js(`document.querySelector('[data-page=knowledge]').click()`);
  await clickText('项目资料 · 自制样本');
  await wait(`document.querySelector('.upload-zone input[type=file]')`);
  await setFile('.upload-zone input[type=file]', path.join(root, 'backend/testdata/engineering/handbook-sections.md'));
  await wait(`!![...document.querySelectorAll('tbody tr')].find(row=>row.textContent.includes('handbook-sections.md') && row.textContent.includes('已就绪'))`, 120000);
  await rowButton('handbook-sections.md', '分块');
  await wait(`document.querySelector('#chunks-dialog[open]') && document.querySelectorAll('#chunks-content .chunk-card').length >= 3`);
  const chunkView = await js(`({
    cards: document.querySelectorAll('#chunks-content .chunk-card').length,
    segments: document.querySelectorAll('#chunks-content .chunk-seg').length,
    sourceSegments: document.querySelectorAll('#chunks-content .chunk-source-seg').length,
    stats: [...document.querySelectorAll('#chunks-content .chunk-stat strong')].map(node => node.textContent),
    firstTag: document.querySelector('#chunks-content .chunk-tag')?.textContent,
    overlapWarned: document.querySelector('.chunk-map-hint')?.textContent.includes('等宽') || false,
  })`);
  assert.equal(chunkView.cards, chunkView.segments, '覆盖条必须与明细一一对应');
  assert.ok(Number(chunkView.stats[0]) >= chunkView.cards, '总数不应小于已展示块数');
  assert.equal(chunkView.overlapWarned, false, '有字符偏移时不应退化为等宽展示');
  if (process.env.KS_UI_RAGFLOW === '1') assert.ok(chunkView.sourceSegments > 0, '组合分块必须绘制实际来源区间');
  assert.equal(chunkView.firstTag, '#1');
  await shot('chunks');
  await js(`document.querySelector('#chunks-content .chunk-toggle').click()`);
  await wait(`document.querySelector('#chunks-content .chunk-card.expanded') && document.querySelector('#chunks-content .chunk-toggle').getAttribute('aria-expanded') === 'true'`);
  await shot('chunks-expanded');
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 });
  await send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 });
  await wait(`!document.querySelector('#chunks-dialog').open`);
  console.log('[浏览器 4/9] 提问并打开服务端引用原文');
  await js(`document.querySelector('[data-page=knowledge]').click()`);
  await clickText('行政制度 · 自制样本');
  // 从「在此知识库提问」进入，检索范围才锁定到当前库；点侧栏「对话」会退化为全部知识库。
  await clickText('在此知识库提问');
  await wait(`document.querySelector('.chat-compose textarea')`);
  // 每轮验收用新会话，避免把本轮问题追加到历史会话里。
  await js(`document.querySelector('#new-chat').click()`);
  await wait(`document.querySelector('.chat-compose textarea')`);
  await shot('chat');
  await js(`document.querySelector('.chat-compose textarea').value='会议室使用后需要做什么？'; document.querySelector('.chat-compose').requestSubmit()`);
  await wait(`[...document.querySelectorAll('.answer-body')].at(-1)?.textContent.includes('关闭照明')`);
  await js(`document.querySelector('.citation-chip').click()`);
  await wait(`document.querySelector('.source-text')?.textContent.includes('关闭照明')`);
  await shot('answer-source');
  await send('Page.bringToFront');
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 });
  await send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 });
  await wait(`!document.querySelector('#source-dialog').open`);
  console.log('[导航状态] 只有点击历史会话时高亮；页面入口不得残留历史选中');
  await js(`document.querySelector('.session-item').click()`);
  await wait(`document.querySelectorAll('.session-item.active').length === 1`);
  for (const page of ['files', 'chat', 'knowledge', 'search']) {
    await js(`document.querySelector('[data-page=${page}]').click()`);
    assert.equal(await js(`document.querySelectorAll('.session-item.active').length`), 0);
    assert.equal(await js(`document.querySelectorAll('.session-item[aria-current]').length`), 0);
  }
  await js(`document.querySelector('[data-page=chat]').click()`);
  console.log('[浏览器 5/9] 库外问题显示无证据，检索测试展示真实片段');
  await js(`document.querySelector('.chat-compose textarea').value='量子纠缠星际导航协议是什么？'; document.querySelector('.chat-compose').requestSubmit()`);
  await wait(`[...document.querySelectorAll('.answer-message')].at(-1)?.textContent.includes('未找到回答依据')`);
  console.log('[故障界面] 通过浏览器拦截注入 HTTP 502，验证不会显示成无证据');
  const intercept = async event => {
    const message = JSON.parse(event.data);
    if (message.method === 'Fetch.requestPaused') await send('Fetch.fulfillRequest', {
      requestId: message.params.requestId, responseCode: 502,
      responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
      body: Buffer.from(JSON.stringify({ error: { code: 'CITATION_VALIDATION_FAILED' }, meta: { request_id: 'synthetic-browser-fault' } })).toString('base64'),
    });
  };
  socket.addEventListener('message', intercept);
  await send('Fetch.enable', { patterns: [{ urlPattern: '*/api/v1/qa', requestStage: 'Request' }] });
  await js(`document.querySelector('.chat-compose textarea').value='自制故障测试'; document.querySelector('.chat-compose').requestSubmit()`);
  await wait(`[...document.querySelectorAll('.answer-message')].at(-1)?.textContent.includes('答案未通过引用校验')`);
  assert.equal(await js(`[...document.querySelectorAll('.answer-message')].at(-1).querySelector('.answer-body')`), null);
  await send('Fetch.disable'); socket.removeEventListener('message', intercept);
  await js(`document.querySelector('[data-page=search]').click()`);
  await wait(`document.querySelector('.search-page h1')?.textContent==='资料检索'`);
  assert.ok(await js(`document.querySelector('#search-query')`), '检索页应直接提供检索输入框');
  await js(`document.querySelector('[data-page=knowledge]').click()`);
  await clickText('行政制度 · 自制样本');
  await clickText('检索');
  await wait(`document.querySelector('#search-query')`);
  await js(`document.querySelector('.query-box textarea').value='会议室使用后需要做什么？'; document.querySelector('.query-box').requestSubmit()`);
  await wait(`document.querySelector('.result-card')`); await shot('retrieval');
  if (process.env.KS_UI_RAGFLOW === '1') {
    assert.ok(await js(`document.querySelector('.search-page').textContent.includes('混合相关度')`),
      '新检索方案应显示混合相关度');
  }
  assert.ok(await js(`document.querySelector('.result-card .result-kb')?.textContent.includes('行政制度')`),
    '检索结果应标注所属知识库');
  assert.equal(await js(`getComputedStyle(document.querySelector('.search-layout')).flexDirection`), 'column',
    '检索页应为上下分布');
  console.log('[浏览器 6/9] 小屏布局无横向溢出');
  await send('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await wait(`getComputedStyle(document.querySelector('.mobile-bar')).display === 'flex' && document.querySelector('.sidebar').getBoundingClientRect().right <= 1`);
  assert.ok(await js(`document.documentElement.scrollWidth <= innerWidth`)); await shot('mobile');
  await send('Emulation.clearDeviceMetricsOverride');
  console.log('[浏览器 7/9] 文档重建、版本更新和删除经真实接口生效');
  await js(`document.querySelector('[data-page=knowledge]').click()`); await clickText('行政制度 · 自制样本');
  await wait(`!![...document.querySelectorAll('tbody tr')].find(row=>row.textContent.includes('expense.md'))`);
  await rowButton('expense.md', '重建');
  await wait(`!![...document.querySelectorAll('tbody tr')].find(row=>row.textContent.includes('expense.md') && row.textContent.includes('代次 2') && row.textContent.includes('已就绪'))`, 120000);
  await shot('documents');
  await rowButton('expense.md', '删除');
  await js(`document.querySelector('#action-form').requestSubmit()`);
  await wait(`!document.querySelector('#action-dialog').open && ![...document.querySelectorAll('tbody tr')].some(row=>row.textContent.includes('expense.md'))`);
  const updatedDom = await send('DOM.getDocument');
  const updateInput = await send('DOM.querySelector', { nodeId: updatedDom.root.nodeId, selector: 'input[aria-label="更新 meeting-room.md"]' });
  await send('DOM.setFileInputFiles', { nodeId: updateInput.nodeId, files: [path.join(root, 'backend/testdata/engineering/e2e-room-v2.md')] });
  await wait(`!![...document.querySelectorAll('tbody tr')].find(row=>row.textContent.includes('meeting-room.md') && row.textContent.includes('V2') && row.textContent.includes('已就绪'))`, 120000);
  await clickText('在此知识库提问');
  await wait(`document.querySelector('.chat-compose textarea')`);
  await js(`document.querySelector('.chat-compose textarea').value='会议室使用后需要做什么？'; document.querySelector('.chat-compose').requestSubmit()`);
  await wait(`[...document.querySelectorAll('.answer-body')].at(-1)?.textContent.includes('关闭空调')`);
  console.log('[实时更新] 全部文件自动接收其他客户端新增库、上传、完成及删除');
  const startup = new URL(process.env.KS_UI_URL);
  const authorization = `Bearer ${decodeURIComponent(startup.hash.slice(3))}`;
  async function apiRequest(endpoint, options = {}) {
    const response = await fetch(new URL('/api/v1' + endpoint, startup), {
      ...options, headers: { Authorization: authorization, ...options.headers },
    });
    assert.ok(response.ok, `自制实时验收接口应成功 ${response.status}`);
    return (await response.json()).data;
  }
  await js(`document.querySelector('[data-page=files]').click()`);
  await wait(`document.querySelector('#file-list tbody tr') && document.querySelector('#live-status').hidden`);
  await shot('files');
  // 行内操作只保留浏览与分块，所属知识库在「所属知识库」列里已可点击。
  assert.equal(await js(`[...document.querySelectorAll('#file-list tbody tr')].every(row => ![...row.querySelectorAll('button')].some(n => n.textContent === '所在知识库'))`), true);
  const pushedKb = await apiRequest('/knowledge-bases', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: '实时同步 · 自制样本' }),
  });
  const form = new FormData();
  form.append('file', new Blob(['完全自制的实时同步验收内容。']), '实时刷新.txt');
  const pushed = await apiRequest(`/knowledge-bases/${pushedKb.id}/documents`, { method: 'POST', body: form });
  await wait(`!![...document.querySelectorAll('#file-list tbody tr')].find(row => row.textContent.includes('实时刷新.txt') && row.textContent.includes('已就绪'))`, 120000);
  let listReads = 0;
  const countReads = event => {
    const message = JSON.parse(event.data);
    if (message.method === 'Network.requestWillBeSent' && /\/knowledge-bases(?:\/[^/]+\/documents)?$/.test(new URL(message.params.request.url).pathname)) listReads += 1;
  };
  socket.addEventListener('message', countReads);
  await send('Network.enable');
  await delay(600);
  const idleReads = listReads;
  await delay(3000);
  assert.equal(listReads, idleReads, '没有资料变化时不应定时 GET 列表');
  socket.removeEventListener('message', countReads);
  await clickText('实时刷新.txt');
  await wait(`document.querySelector('.browse-page') && document.querySelector('.browse-block')`);
  await js(`document.querySelector('.browse-head .back-link').click()`);
  await wait(`document.querySelector('#file-list tbody tr') && document.querySelector('#live-status').hidden`);
  await apiRequest(`/documents/${pushed.document_id}`, { method: 'DELETE' });
  await wait(`![...document.querySelectorAll('#file-list tbody tr')].some(row => row.textContent.includes('实时刷新.txt'))`);
  console.log('[浏览器 8/9] 文件名/库名作为文本渲染，不执行 HTML');
  await js(`document.querySelector('[data-page=knowledge]').click()`); await clickText('新建知识库');
  await js(`document.querySelector('#action-input').value='<img src=x onerror="window.uiXss=true">'; document.querySelector('#action-form').requestSubmit()`);
  await wait(`!document.querySelector('#action-dialog').open && document.querySelectorAll('.kb-card').length===${kbBefore + 3}`);
  assert.equal(await js(`Boolean(window.uiXss || document.querySelector('.kb-card img'))`), false);
  console.log('[浏览器 9/9] 刷新清空身份和内容，浏览器存储中没有令牌');
  assert.equal(await js(`localStorage.length + sessionStorage.length`), 0);
  await send('Page.reload'); await wait(`document.querySelector('#connection-dialog[open]')`);
  assert.equal(await js(`document.querySelectorAll('.kb-card').length`), 0);
  console.log('[浏览器 补充] 重连后从后端恢复对话历史');
  // 仅改 hash 不会重新执行 bootstrap；加一个 query 强制整页加载，令牌仍在片段里。
  await send('Page.navigate', {
    url: process.env.KS_UI_URL.replace('/workbench/', '/workbench/?reconnect=1'),
  });
  await wait(`document.querySelector('#app').dataset.connected === 'true'`);
  await js(`document.querySelector('[data-page=chat]').click()`);
  await wait(`document.querySelectorAll('#thread .turn').length >= 3`);
  assert.ok(await js(`[...document.querySelectorAll('.answer-body')].at(-1)?.textContent.includes('关闭空调')`),
    '重连后最后一条回答应仍是 v2 内容');
  assert.ok(await js(`document.querySelectorAll('#session-list .session-item').length >= 1`));
  await shot('history');
  console.log('[身份清理] 从文件预览断开连接，清空原件、正文与抽屉');
  await js(`document.querySelector('[data-page=files]').click()`);
  await wait(`!![...document.querySelectorAll('#file-list tbody tr')].find(row=>row.textContent.includes('meeting-room.md'))`);
  await rowButton('meeting-room.md', '浏览');
  await wait(`document.querySelector('.browse-page') && document.querySelector('.browse-block')`);
  await js(`document.querySelector('#connection').click()`);
  await clickText('断开连接');
  await wait(`document.querySelector('#app').dataset.connected === 'false'`);
  assert.equal(await js(`document.querySelector('.browse-page')`), null);
  assert.equal(await js(`document.querySelector('#source-content').textContent`), '');
  assert.equal(await js(`document.querySelector('#chunks-content').textContent`), '');
  assert.equal(await js(`document.querySelectorAll('#session-list .session-item').length`), 0);
  console.log(`PASS: 浏览器全链路验证；截图目录 ${output}`);
} finally {
  socket?.close(); browser.kill();
}
