// SSE 协议与连接生命周期。事件只使缓存失效，实际数据继续由授权 REST 获取。
export async function readEvents(body, onEvent, onActivity = () => {}) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let event = 'message';
  let data = [];
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) return;
      onActivity();
      buffer += decoder.decode(value, { stream: true });
      if (buffer.length > 16384) throw new Error('实时事件超出协议上限');
      let end;
      while ((end = buffer.indexOf('\n')) !== -1) {
        const line = buffer.slice(0, end).replace(/\r$/, '');
        buffer = buffer.slice(end + 1);
        if (!line) {
          if (data.length) onEvent(event, JSON.parse(data.join('\n')));
          event = 'message';
          data = [];
        } else if (line.startsWith('event:')) event = line.slice(6).trim();
        else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
        if (data.join('\n').length > 16384) throw new Error('实时事件超出协议上限');
      }
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

export class DocumentEvents {
  constructor(api, { onChange, onStatus }) {
    this.api = api;
    this.onChange = onChange;
    this.onStatus = onStatus;
    this.running = false;
    this.generation = 0;
    this.retryDelay = 1000;
  }
  start() {
    if (this.running) return;
    this.running = true;
    this.retryDelay = 1000;
    this.connect();
  }
  stop() {
    this.running = false;
    this.generation += 1;
    clearTimeout(this.retryTimer);
    clearTimeout(this.watchdog);
    this.controller?.abort();
  }
  async connect() {
    const generation = ++this.generation;
    const revision = this.api.revision;
    const controller = new AbortController();
    this.controller = controller;
    const current = () => this.running && generation === this.generation && revision === this.api.revision;
    const touch = () => {
      clearTimeout(this.watchdog);
      this.watchdog = setTimeout(() => controller.abort(), 45000);
    };
    this.onStatus('connecting');
    touch();
    try {
      const headers = { Accept: 'text/event-stream', Authorization: `Bearer ${this.api.token}` };
      const response = await this.api.fetcher('/api/v1/events', {
        headers, signal: controller.signal, cache: 'no-store', credentials: 'omit',
      });
      if (!current()) { await response.body?.cancel(); return; }
      if (response.status === 401) {
        await response.body?.cancel();
        this.stop(); this.onStatus('expired'); return;
      }
      if (!response.ok || !response.body || !response.headers.get('content-type')?.includes('text/event-stream')) {
        await response.body?.cancel();
        throw new Error('实时连接未就绪');
      }
      await readEvents(response.body, (kind, data) => {
        if (!current()) return;
        if (data?.version !== 1) throw new Error('实时事件版本不兼容');
        if (kind === 'expired') { this.stop(); this.onStatus('expired'); return; }
        if (kind === 'sync' || kind === 'change') {
          this.retryDelay = 1000;
          this.onStatus('connected');
          this.onChange();
        }
      }, () => { if (current()) touch(); });
    } catch {
      // 失败通过连接状态和有界退避明确呈现，不用列表轮询伪装实时连接。
    } finally {
      if (generation === this.generation) clearTimeout(this.watchdog);
      if (current()) {
        controller.abort();
        this.onStatus('retrying');
        this.retryTimer = setTimeout(() => this.connect(), this.retryDelay);
        this.retryDelay = Math.min(this.retryDelay * 2, 30000);
      }
    }
  }
}
