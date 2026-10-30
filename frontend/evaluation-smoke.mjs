// 只读验收真实评测页面，不提交新问题、不新增模型调用；使用隔离浏览器配置。
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const output = path.join(root, 'backend/.data/workbench/evaluation-preview');
await mkdir(output, { recursive: true });
assert.ok(process.env.KS_UI_BROWSER && process.env.KS_UI_URL);
const browser = spawn(process.env.KS_UI_BROWSER, ['--headless=new', '--remote-debugging-port=0',
  `--user-data-dir=${path.join(output, 'profile-' + Date.now())}`, '--no-first-run', '--no-default-browser-check',
  '--disable-background-networking', '--disable-sync', '--no-proxy-server', '--window-size=1440,1100',
  process.env.KS_UI_URL], { stdio: 'ignore', windowsHide: true });
const profile = browser.spawnargs.find(item => item.startsWith('--user-data-dir=')).slice('--user-data-dir='.length);
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(read) {
  const end = Date.now() + 30000;
  while (Date.now() < end) { try { const result = await read(); if (result) return result; } catch {} await delay(150); }
  throw new Error('评测页面未进入预期状态');
}
let socket;
try {
  const lines = await until(async () => (await readFile(path.join(profile, 'DevToolsActivePort'), 'utf8')).trim().split('\n'));
  const targets = await (await fetch(`http://127.0.0.1:${lines[0]}/json/list`)).json();
  socket = new WebSocket(targets.find(item => item.type === 'page').webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener('open', resolve); socket.addEventListener('error', reject); });
  let next = 0; const pending = new Map();
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data); const item = pending.get(message.id); if (!item) return;
    pending.delete(message.id); message.error ? item.reject(new Error('浏览器命令失败')) : item.resolve(message.result);
  });
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++next; pending.set(id, { resolve, reject }); socket.send(JSON.stringify({ id, method, params }));
  });
  const js = async expression => {
    const result = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
    if (result.exceptionDetails) throw new Error('评测页面脚本异常'); return result.result.value;
  };
  const shot = async name => {
    const data = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
    await writeFile(path.join(output, name + '.png'), Buffer.from(data.data, 'base64'));
  };
  await send('Page.enable'); await send('Runtime.enable');
  await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-reduced-motion', value: 'reduce' }] });
  await until(() => js(`document.querySelector('#connection-label')?.textContent.startsWith('已连接')`));
  await js(`document.querySelector('[data-page=search]').click()`);
  await until(() => js(`document.querySelectorAll('.eval-case').length===24`));
  assert.equal(await js(`document.querySelectorAll('.eval-table')[0].querySelectorAll('tbody tr').length`), 4);
  assert.equal(await js(`document.querySelectorAll('.eval-table')[1].querySelectorAll('tbody tr').length`), 8);
  assert.equal(await js(`document.querySelector('#search-query')`), null);
  assert.equal(await js(`location.hash.includes('t=')`), false);
  await until(() => js(`document.querySelector('#notice').hidden`));
  await shot('overview');
  await js(`[...document.querySelectorAll('.evaluation-content h2')].find(item=>item.textContent==='分维度概览').scrollIntoView()`);
  await shot('dimensions');
  await js(`document.querySelector('.eval-filter').value='表格定位';document.querySelector('.eval-filter').dispatchEvent(new Event('change'));[...document.querySelectorAll('.eval-case')].slice(1).forEach(item=>item.open=true);document.querySelectorAll('.eval-case')[1].scrollIntoView()`);
  await shot('table-failures');
  await js(`document.querySelector('.eval-filter').value='跨文档证据';document.querySelector('.eval-filter').dispatchEvent(new Event('change'))`);
  assert.equal(await js(`document.querySelectorAll('.eval-case').length`), 3);
  await js(`document.querySelector('.eval-case').open=true;document.querySelector('.eval-case').scrollIntoView()`);
  await shot('case');
  await js(`[...document.querySelectorAll('.eval-case button')].find(item=>item.textContent.includes('在知识库中复测')).click()`);
  await until(() => js(`document.querySelector('#search-query')?.value.includes('青岚项目')`));
  assert.ok(await js(`document.querySelector('.tabs .active')?.textContent.includes('检索')`));
  assert.equal(await js(`document.querySelector('.search-options .scope-btn')`), null, '库内检索不允许偷偷扩大范围');
  await shot('library-search');
  await js(`document.querySelector('[data-page=search]').click()`);
  await until(() => js(`document.querySelectorAll('.eval-case').length===24`));
  await js(`[...document.querySelectorAll('.eval-overview button')].find(item=>item.textContent.includes('在评测资料中提问')).click()`);
  await until(() => js(`document.querySelector('.chat-compose')`));
  assert.ok(await js(`document.querySelector('.scope-label')?.textContent.includes('基础检索评测')`));
  assert.equal(await js(`document.querySelector('.chat-compose textarea').value`), '');
  await js(`document.querySelector('[data-page=search]').click()`);
  await until(() => js(`document.querySelectorAll('.eval-case').length===24`));
  await js(`document.querySelector('.eval-case').open=true;[...document.querySelectorAll('.eval-case button')].find(item=>item.textContent.includes('查看实际问答')).click()`);
  await until(() => js(`document.querySelectorAll('.answer-message').length===3`));
  assert.ok(await js(`document.querySelectorAll('.citation-chip').length>0`));
  await js(`document.querySelector('.citation-chip').click()`);
  await until(() => js(`document.querySelector('#source-dialog').open`));
  await js(`document.querySelector('#source-dialog').close()`);
  await shot('history');
  await js(`document.querySelector('[data-page=search]').click()`);
  await until(() => js(`document.querySelectorAll('.eval-case').length===24`));
  await send('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await until(() => js(`document.querySelector('.sidebar').getBoundingClientRect().right<=1`));
  assert.ok(await js(`document.documentElement.scrollWidth<=innerWidth`));
  await shot('mobile');
  console.log(`PASS: 24题、8维度、4方案、维度筛选、知识库复测入口及移动布局；无新增模型调用。截图：${output}`);
} finally {
  socket?.close(); browser.kill();
}
