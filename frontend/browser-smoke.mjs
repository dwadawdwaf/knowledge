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
  async function wait(expression) { return until(() => js(expression), '页面没有进入预期状态'); }
  async function shot(name) { const image = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false }); await writeFile(path.join(output, name + '.png'), Buffer.from(image.data, 'base64')); }
  const clickText = text => js(`[...document.querySelectorAll('button')].find(n=>n.textContent.trim()===${JSON.stringify(text)}).click()`);
  async function setFile(selector, file) { const dom = await send('DOM.getDocument'); const node = await send('DOM.querySelector', { nodeId: dom.root.nodeId, selector }); assert.ok(node.nodeId, '文件输入框未找到'); await send('DOM.setFileInputFiles', { nodeId: node.nodeId, files: [file] }); }
  const rowButton = (file, label) => js(`(()=>{const row=[...document.querySelectorAll('tbody tr')].find(r=>r.textContent.includes(${JSON.stringify(file)}));const target=[...row.querySelectorAll('button')].find(n=>n.textContent.trim()===${JSON.stringify(label)});target.click();return true})()`);
  await send('Page.enable'); await send('Runtime.enable');
  await wait(`document.querySelector('#content .primary')`);
  console.log('[浏览器 1/9] 打开带令牌的地址即自动连接，地址栏随即清除令牌');
  await wait(`document.querySelector('#connection-label')?.textContent.startsWith('已连接')`);
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
  await setFile('.upload-zone input[type=file]', path.join(root, 'backend/testdata/evaluation/expense.md'));
  await wait(`!![...document.querySelectorAll('tbody tr')].find(row=>row.textContent.includes('expense.md') && row.textContent.includes('已就绪'))`);
  console.log('[浏览器 3/9] 打开分块结果，核对覆盖条与明细一一对应');
  await setFile('.upload-zone input[type=file]', path.join(root, 'backend/testdata/engineering/handbook-sections.md'));
  await wait(`!![...document.querySelectorAll('tbody tr')].find(row=>row.textContent.includes('handbook-sections.md') && row.textContent.includes('已就绪'))`);
  await rowButton('handbook-sections.md', '分块');
  await wait(`document.querySelector('#chunks-dialog[open]') && document.querySelectorAll('#chunks-content .chunk-card').length >= 3`);
  const chunkView = await js(`({
    cards: document.querySelectorAll('#chunks-content .chunk-card').length,
    segments: document.querySelectorAll('#chunks-content .chunk-seg').length,
    stats: [...document.querySelectorAll('#chunks-content .chunk-stat strong')].map(node => node.textContent),
    firstTag: document.querySelector('#chunks-content .chunk-tag')?.textContent,
    overlapWarned: document.querySelector('.chunk-map-hint')?.textContent.includes('等宽') || false,
  })`);
  assert.equal(chunkView.cards, chunkView.segments, '覆盖条必须与明细一一对应');
  assert.ok(Number(chunkView.stats[0]) >= chunkView.cards, '总数不应小于已展示块数');
  assert.equal(chunkView.overlapWarned, false, '有字符偏移时不应退化为等宽展示');
  assert.equal(chunkView.firstTag, '#1');
  await shot('chunks');
  await js(`document.querySelector('#chunks-content .chunk-toggle').click()`);
  await wait(`document.querySelector('#chunks-content .chunk-card.expanded') && document.querySelector('#chunks-content .chunk-toggle').getAttribute('aria-expanded') === 'true'`);
  await shot('chunks-expanded');
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 });
  await send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 });
  await wait(`!document.querySelector('#chunks-dialog').open`);
  console.log('[浏览器 4/9] 提问并打开服务端引用原文');
  await js(`document.querySelector('[data-page=chat]').click()`);
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
  await js(`document.querySelector('[data-page=search]').click(); document.querySelector('.query-box textarea').value='会议室使用后需要做什么？'; document.querySelector('.query-box').requestSubmit()`);
  await wait(`document.querySelector('.result-card')`); await shot('retrieval');
  console.log('[浏览器 6/9] 小屏布局无横向溢出');
  await send('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  assert.ok(await js(`document.documentElement.scrollWidth <= innerWidth`)); await shot('mobile');
  await send('Emulation.clearDeviceMetricsOverride');
  console.log('[浏览器 7/9] 文档重建、版本更新和删除经真实接口生效');
  await js(`document.querySelector('[data-page=knowledge]').click()`); await clickText('行政制度 · 自制样本');
  await wait(`!![...document.querySelectorAll('tbody tr')].find(row=>row.textContent.includes('expense.md'))`);
  await rowButton('expense.md', '重建');
  await wait(`!![...document.querySelectorAll('tbody tr')].find(row=>row.textContent.includes('expense.md') && row.textContent.includes('代次 2') && row.textContent.includes('已就绪'))`);
  await shot('documents');
  await rowButton('expense.md', '删除');
  await js(`document.querySelector('#action-form').requestSubmit()`);
  await wait(`!document.querySelector('#action-dialog').open && ![...document.querySelectorAll('tbody tr')].some(row=>row.textContent.includes('expense.md'))`);
  const updatedDom = await send('DOM.getDocument');
  const updateInput = await send('DOM.querySelector', { nodeId: updatedDom.root.nodeId, selector: 'input[aria-label="更新 meeting-room.md"]' });
  await send('DOM.setFileInputFiles', { nodeId: updateInput.nodeId, files: [path.join(root, 'backend/testdata/engineering/e2e-room-v2.md')] });
  await wait(`!![...document.querySelectorAll('tbody tr')].find(row=>row.textContent.includes('meeting-room.md') && row.textContent.includes('V2') && row.textContent.includes('已就绪'))`);
  await js(`document.querySelector('[data-page=chat]').click(); document.querySelector('.chat-compose textarea').value='会议室使用后需要做什么？'; document.querySelector('.chat-compose').requestSubmit()`);
  await wait(`[...document.querySelectorAll('.answer-body')].at(-1)?.textContent.includes('关闭空调')`);
  console.log('[浏览器 8/9] 文件名/库名作为文本渲染，不执行 HTML');
  await js(`document.querySelector('[data-page=knowledge]').click()`); await clickText('新建知识库');
  await js(`document.querySelector('#action-input').value='<img src=x onerror="window.uiXss=true">'; document.querySelector('#action-form').requestSubmit()`);
  await wait(`!document.querySelector('#action-dialog').open && document.querySelectorAll('.kb-card').length===${kbBefore + 2}`);
  assert.equal(await js(`Boolean(window.uiXss || document.querySelector('.kb-card img'))`), false);
  console.log('[浏览器 9/9] 刷新清空身份和内容，浏览器存储中没有令牌');
  assert.equal(await js(`localStorage.length + sessionStorage.length`), 0);
  await send('Page.reload'); await wait(`document.querySelector('#connection-label')?.textContent==='未连接'`);
  assert.equal(await js(`document.querySelectorAll('.kb-card').length`), 0);
  console.log(`PASS: 浏览器全链路验证；截图目录 ${output}`);
} finally {
  socket?.close(); browser.kill();
}
