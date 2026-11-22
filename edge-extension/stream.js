'use strict';

(() => {
  if (window.__desktopAiStreamInstalled) return;
  window.__desktopAiStreamInstalled = true;

  const completionPath = '/api/v0/chat/completion';
  const xhrUrl = new WeakMap();
  const open = XMLHttpRequest.prototype.open;
  const send = XMLHttpRequest.prototype.send;
  let armed;
  let running;

  function render(job, type, extra = {}) {
    window.dispatchEvent(new CustomEvent('desktop-ai:stream', {
      detail: { id: job.id, type, ...extra }
    }));
  }

  function sessionUrl() {
    return /^\/a\/chat\/s\/[a-f0-9-]{36}$/i.test(location.pathname) ? location.origin + location.pathname : '';
  }

  function resetPath() {
    return { op: 'SET', path: '' };
  }

  function applyPatch(job, patch, cursor, prefix = '') {
    if (!patch || typeof patch !== 'object') return;
    if (typeof patch.p === 'string') cursor.path = patch.p;
    if (typeof patch.o === 'string') cursor.op = patch.o;
    const path = (prefix + cursor.path).split('/').filter(Boolean);
    if (path.some(key => ['__proto__', 'prototype', 'constructor'].includes(key))) return;
    if (cursor.op === 'BATCH' && Array.isArray(patch.v)) {
      const next = resetPath();
      const parent = path.length ? path.join('/') + '/' : '';
      for (const child of patch.v) applyPatch(job, child, next, parent);
      return;
    }
    if (!path.length) {
      if (cursor.op === 'SET' && patch.v && typeof patch.v === 'object') job.state = patch.v;
      return;
    }
    let node = job.state;
    for (let index = 0; index < path.length - 1; index += 1) {
      const key = Array.isArray(node) && path[index] === '-1' ? node.length - 1 : path[index];
      if (key === -1) return;
      if (!node[key] || typeof node[key] !== 'object') {
        node[key] = /^-?\d+$/.test(path[index + 1]) ? [] : {};
      }
      node = node[key];
    }
    const last = path.at(-1);
    const key = Array.isArray(node) && last === '-1' ? node.length - 1 : last;
    if (key === -1) return;
    if (cursor.op === 'SET') {
      node[key] = patch.v;
    } else if (cursor.op === 'APPEND' && Array.isArray(patch.v)) {
      node[key] = (Array.isArray(node[key]) ? node[key] : []).concat(patch.v);
    } else if (cursor.op === 'APPEND' && typeof patch.v === 'string') {
      node[key] = (typeof node[key] === 'string' ? node[key] : '') + patch.v;
    }
  }

  function update(job) {
    const url = sessionUrl();
    if (url && url !== job.url) {
      job.url = url;
      render(job, 'session', { url, parent: job.parent });
    }
    const fragments = job.state.response?.fragments ?? job.state.fragments;
    if (!Array.isArray(fragments)) return;
    const ofType = types => fragments
      .filter(item => item && types.includes(item.type) && typeof item.content === 'string')
      .map(item => item.content).join('\n');
    const answer = ofType(['RESPONSE', 'TEMPLATE_RESPONSE']).slice(0, 120000);
    const thought = ofType(['THINK']).slice(0, 120000);
    const searches = fragments.filter(item => item && ['SEARCH', 'TOOL_SEARCH'].includes(item.type));
    const searching = searches.some(item => item.status === 'WIP' ||
      (!item.status && !((item.results?.length ?? 0) > 0)));
    const search = searches.length ? (searching && !answer && !job.finished ? 'searching' : 'done') : '';
    if (answer !== job.answer || thought !== job.thought || search !== job.search) {
      Object.assign(job, { answer, thought, search });
      render(job, 'delta', { answer, thought, search });
    }
  }

  function processEvent(job, record) {
    let name = '';
    const data = [];
    for (const line of record.split(/\r?\n/)) {
      if (line.startsWith('event:')) name = line.slice(6).trim();
      if (line.startsWith('data:')) data.push(line.slice(5).trimStart());
    }
    if (name === 'finish' || name === 'close') {
      job.finished = true;
      return;
    }
    if (!data.length) return;
    if (data.join('\n') === '[DONE]') {
      job.finished = true;
      return;
    }
    let value;
    try { value = JSON.parse(data.join('\n')); } catch { return; }
    if (!value || typeof value !== 'object') return;
    if (name === 'ready') {
      if (typeof value.response_message_id === 'number') job.parent = value.response_message_id;
    } else if (name === 'hint' || name === 'toast') {
      if (value.type === 'error' && typeof value.content === 'string') job.error = value.content.slice(0, 300);
    } else if (!name || name === 'delta' || name === 'message') {
      if (Object.hasOwn(value, 'v')) applyPatch(job, value, job.cursor);
    }
  }

  function read(job, final = false) {
    let response;
    try { response = job.xhr.responseText; } catch { return; }
    if (typeof response !== 'string') return;
    job.pending += response.slice(job.offset);
    job.offset = response.length;
    const records = job.pending.split(/\r?\n\r?\n/);
    job.pending = records.pop();
    for (const event of records) processEvent(job, event);
    if (final && job.pending) processEvent(job, job.pending);
    update(job);
  }

  function ended(job) {
    if (running !== job) return;
    running = null;
    read(job, true);
    const status = job.xhr.status;
    if (job.stopped) {
      render(job, 'stopped', { url: sessionUrl() || job.url, parent: job.parent });
    } else if (status === 401 || status === 403) {
      render(job, 'login', { text: '请在 Edge 确认登录状态或完成验证。' });
    } else if (job.error || status < 200 || status >= 300 || !job.answer.trim()) {
      render(job, 'error', { text: job.error || '网页没有返回可显示的正文，请在 Edge 查看。' });
    } else if (!job.finished) {
      render(job, 'error', { text: '网页连接中断。已保留收到的内容。' });
    } else {
      render(job, 'complete', { url: sessionUrl() || job.url, parent: job.parent });
    }
  }

  window.addEventListener('desktop-ai:arm', ({ detail }) => {
    if (typeof detail?.id === 'string' && typeof detail.prompt === 'string') armed = detail;
  });
  window.addEventListener('desktop-ai:disarm', ({ detail }) => {
    if (armed?.id === detail?.id) armed = null;
  });
  window.addEventListener('desktop-ai:abort', ({ detail }) => {
    if (armed?.id === detail?.id) armed = null;
    if (running?.id !== detail?.id) return;
    const job = running;
    job.stopped = true;
    job.xhr.abort();
  });

  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    xhrUrl.set(this, new URL(url, location.href));
    return open.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.send = function (body) {
    const url = xhrUrl.get(this);
    if (armed && url?.origin === location.origin && url.pathname === completionPath) {
      let prompt;
      try { prompt = JSON.parse(body).prompt; } catch { /* Other requests remain untouched. */ }
      if (prompt === armed.prompt) {
        const job = {
          id: armed.id, xhr: this, state: {}, cursor: resetPath(), offset: 0, pending: '',
          answer: '', thought: '', search: '', url: '', parent: null
        };
        armed = null;
        running = job;
        this.addEventListener('progress', () => { if (running === job) read(job); });
        this.addEventListener('loadend', () => ended(job), { once: true });
        render(job, 'started');
      }
    }
    return send.call(this, body);
  };
})();
