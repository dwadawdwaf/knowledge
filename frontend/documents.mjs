// 资料数据与失效同步。视图只读共享状态，网络请求和 SSE 生命周期在此收口。
import { DocumentEvents } from './events.mjs';

export function createDocumentData({ api, state, render, drawDocuments, drawFiles, notify, errorText, expired }) {
  let controller = new AbortController();
  let activeKey = '';
  let liveWanted = false;
  let liveRevision = -1;
  let libraryRequest = null;
  let documentRequest = null;
  let filesRequest = null;
  let refreshTimer;
  let refreshing = false;
  let dirty = false;
  let libraryViewDirty = false;
  let documentsViewDirty = false;
  let filesViewDirty = false;

  const events = new DocumentEvents(api, {
    onChange: scheduleRefresh,
    onStatus(status) {
      state.liveStatus = status;
      const node = document.querySelector('#live-status');
      if (node) {
        node.hidden = status === 'connected';
        node.textContent = status === 'retrying'
          ? '实时更新连接中断，正在重连。也可手动刷新。'
          : status === 'expired' ? '连接身份已过期，请重新连接。' : '正在连接实时更新…';
      }
      if (status === 'expired') expired();
    },
  });

  function flushView() {
    if (document.querySelector('dialog[open], .menu')) return;
    if (document.activeElement?.closest('#content input, #content textarea')) return;
    if (libraryViewDirty && !(state.page === 'knowledge' && state.library && state.busy)) {
      libraryViewDirty = false;
      if (state.page === 'knowledge' || state.page === 'files') render();
    }
    if (documentsViewDirty && state.page === 'knowledge' && state.library) drawList(drawDocuments);
    if (filesViewDirty && state.page === 'files') drawList(drawFiles);
    documentsViewDirty = false;
    filesViewDirty = false;
  }

  function drawList(draw) {
    const focused = document.activeElement;
    const id = focused?.closest('tr[data-document-id]')?.dataset.documentId;
    const label = focused?.textContent;
    draw();
    if (id && !focused.isConnected) {
      const row = [...document.querySelectorAll('tr[data-document-id]')].find(item => item.dataset.documentId === id);
      const button = [...(row?.querySelectorAll('button') || [])].find(item => item.textContent === label);
      button?.focus({ preventScroll: true });
    }
  }

  async function loadLibraries() {
    if (libraryRequest) return libraryRequest;
    const signal = controller.signal;
    const revision = api.revision;
    const pending = (async () => {
      const result = await api.request('/knowledge-bases', { signal });
      if (signal.aborted || revision !== api.revision) return;
      const libraries = (result.items || []).filter(kb => kb.status === 'active');
      const changed = JSON.stringify(libraries) !== JSON.stringify(state.libraries);
      state.libraries = libraries;
      if (state.scope && !libraries.some(kb => kb.id === state.scope)) state.scope = '';
      if (state.library) state.library = libraries.find(kb => kb.id === state.library.id) || null;
      if (state.fileKb && !libraries.some(kb => kb.id === state.fileKb)) state.fileKb = '';
      if (changed) {
        state.fileLoaded = false;
        libraryViewDirty = true;
      }
    })();
    libraryRequest = pending;
    try { return await pending; }
    finally { if (libraryRequest === pending) libraryRequest = null; }
  }

  async function loadDocuments(quiet = false) {
    const library = state.library;
    if (!library || !state.connected) return;
    if (documentRequest?.id === library.id) return documentRequest.promise;
    const signal = controller.signal;
    const revision = api.revision;
    if (!quiet) { state.docLoading = true; drawDocuments(); }
    const current = () => !signal.aborted && revision === api.revision && state.library?.id === library.id;
    const pending = (async () => {
      try {
        const result = await api.request(`/knowledge-bases/${library.id}/documents`, { timeout: 15000, signal });
        if (!current()) return;
        const items = (result.items || []).filter(doc => doc.status === 'active');
        const signature = JSON.stringify(items);
        if (signature !== state.docSignature || state.docError) documentsViewDirty = true;
        if (signature !== state.docSignature) state.fileLoaded = false;
        state.documents = items;
        state.docSignature = signature;
        state.docError = null;
      } catch (error) {
        if (!current()) return;
        if (error.status === 401) { expired(); return; }
        if (!quiet) notify(errorText(error));
        state.docError = error;
        documentsViewDirty = true;
      } finally {
        if (current()) {
          state.docLoading = false;
          if (!quiet) { drawList(drawDocuments); documentsViewDirty = false; }
          else flushView();
        }
      }
    })();
    documentRequest = { id: library.id, promise: pending };
    try { return await pending; }
    finally { if (documentRequest?.promise === pending) documentRequest = null; }
  }

  async function loadAllFiles(quiet = false) {
    if (!state.connected) return;
    if (filesRequest) return filesRequest;
    const signal = controller.signal;
    const revision = api.revision;
    const current = () => !signal.aborted && revision === api.revision;
    state.fileLoading = true;
    if (!quiet) drawFiles();
    const pending = (async () => {
      try {
        await loadLibraries();
        if (!current()) return;
        const libraries = [...state.libraries];
        const results = new Array(libraries.length);
        let next = 0;
        // 汇总限制为四个并发请求，避免知识库数量直接决定数据库连接峰值。
        await Promise.all(Array.from({ length: Math.min(4, libraries.length) }, async () => {
          while (next < libraries.length && current()) {
            const index = next++;
            const kb = libraries[index];
            try {
              const data = await fetchKbDocuments(kb, signal);
              results[index] = { kb, items: data.items || [] };
            } catch (error) {
              if (error.status === 401 || error.code === 'STALE_SESSION' || error.code === 'CANCELLED') throw error;
              results[index] = { kb, items: [], error };
            }
          }
        }));
        if (!current()) return;
        const errors = results.filter(item => item.error)
          .map(item => ({ name: item.kb.name, message: errorText(item.error) }));
        const files = results.flatMap(({ kb, items }) => items.filter(doc => doc.status === 'active')
          .map(doc => ({ ...doc, kb_name: kb.name })));
        if (JSON.stringify([files, errors]) !== JSON.stringify([state.allFiles, state.fileErrors])) filesViewDirty = true;
        state.allFiles = files;
        state.fileErrors = errors;
      } catch (error) {
        if (!current()) return;
        if (error.status === 401) { expired(); return; }
        state.fileErrors = [{ name: '知识库列表', message: errorText(error) }];
        filesViewDirty = true;
        if (!quiet) notify(errorText(error));
      } finally {
        if (current()) {
          state.fileLoading = false;
          state.fileLoaded = true;
          if (!quiet) { drawList(drawFiles); filesViewDirty = false; }
          flushView();
        }
      }
    })();
    filesRequest = pending;
    try { return await pending; }
    finally { if (filesRequest === pending) filesRequest = null; }
  }

  async function fetchKbDocuments(kb, signal) {
    for (let attempt = 0; ; attempt += 1) {
      signal.throwIfAborted();
      try {
        return await api.request(`/knowledge-bases/${kb.id}/documents`, { timeout: 20000, signal });
      } catch (error) {
        const retryable = (error.status === 0 || error.status >= 500)
          && !['STALE_SESSION', 'CANCELLED'].includes(error.code);
        if (attempt || !retryable || signal.aborted) throw error;
        await new Promise(resolve => setTimeout(resolve, 400));
      }
    }
  }

  function scheduleRefresh() {
    dirty = true;
    if (!liveWanted || refreshTimer || refreshing) return;
    refreshTimer = setTimeout(refresh, 120);
  }

  async function refresh() {
    refreshTimer = null;
    if (!liveWanted || !state.connected) return;
    refreshing = true;
    dirty = false;
    const signal = controller.signal;
    try {
      // 事件可能在一个旧快照读取中到达，先等旧请求结束，再读取事件之后的状态。
      await Promise.allSettled([libraryRequest, documentRequest?.promise, filesRequest].filter(Boolean));
      if (signal.aborted) return;
      if (state.page === 'files') await loadAllFiles(true);
      else {
        await loadLibraries();
        if (!signal.aborted && state.library) await loadDocuments(true);
      }
      if (!signal.aborted) flushView();
    } catch (error) {
      if (!signal.aborted && error.status === 401) expired();
      else if (!signal.aborted) notify(errorText(error));
    } finally {
      refreshing = false;
      if (dirty) scheduleRefresh();
    }
  }

  function stopUpdates() {
    events.stop();
    clearTimeout(refreshTimer);
    refreshTimer = null;
    activeKey = '';
    liveWanted = false;
    liveRevision = -1;
    // 只在身份退出、隐藏或离开页面时真正取消在读请求；切换页面由 updateSubscription 负责。
    controller.abort();
    controller = new AbortController();
    libraryRequest = documentRequest = filesRequest = null;
    state.docLoading = state.fileLoading = false;
  }

  function updateSubscription() {
    const relevant = state.page === 'files'
      || state.page === 'knowledge' && (!state.library || state.libraryTab !== 'search');
    const key = `${api.revision}:${state.page}:${state.library?.id || ''}`;
    const wanted = state.connected && !document.hidden && relevant;
    const changed = key !== activeKey;
    activeKey = key;
    // 订阅只跟随身份与可见性：切页不重建连接，否则每次切换都要取消一条流再重新 LISTEN。
    if (wanted && (!liveWanted || liveRevision !== api.revision)) {
      liveWanted = true;
      liveRevision = api.revision;
      events.stop();
      events.start();
    } else if (!wanted && liveWanted) {
      liveWanted = false;
      events.stop();
    }
    if (changed && wanted) scheduleRefresh();
    flushView();
  }

  return { loadLibraries, loadDocuments, loadAllFiles, updateSubscription, stopUpdates, flushView };
}
