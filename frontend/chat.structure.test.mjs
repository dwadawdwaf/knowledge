// 结构回归：发送与停止必须共用输入区同一个按钮，不能回退成"回答下面再放一个停止按钮"。
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('./chat.mjs', import.meta.url), 'utf8');

test('对话页不再渲染独立的停止按钮', () => {
  assert.equal(source.includes('stop-btn'), false);
  // 停止文案与图标统一由 stream.mjs 的 sendButtonView 提供，避免两处各写一套
  assert.equal(source.includes("'停止生成'"), false);
});

test('输入区只创建一个发送按钮', () => {
  const created = source.match(/el\('button', 'send-btn'\)/g) || [];
  assert.equal(created.length, 1);
});

test('生成中切换到停止时保留同一个按钮元素', () => {
  // 只改 className / aria / 图标，不新建按钮，也不移除原按钮
  assert.ok(source.includes('applySendButton(composerButton, streaming)'));
  assert.ok(source.includes('button.replaceChildren(icon(view.icon))'));
});
