'use strict';

const origin = 'https://chat.deepseek.com';
const home = origin + '/';
const bridgeUrl = 'http://127.0.0.1:41692';
const sessionUrl = /^https:\/\/chat\.deepseek\.com\/a\/chat\/s\/[a-f0-9-]{36}$/i;
const reconnectAlarm = 'desktop-ai-reconnect';
const terminalEvents = ['complete', 'stopped', 'login', 'error'];
const sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
let bridgeToken;
let polling = false;
let eventQueue = Promise.resolve();
let active;
let stopping;

function diagnose(id, stage, httpStatus) {
  if (!bridgeToken || !/^[0-9a-f]{32}$/.test(id)) return;
  const event = { type: 'diagnostic', id, stage };
  if (Number.isInteger(httpStatus)) event.http_status = httpStatus;
  void localRequest('/event', bridgeToken, event, 3000).catch(() => console.warn('Desktop AI:', stage, id));
}

function post(message) {
  eventQueue = eventQueue.catch(() => {}).then(async () => {
    if (!bridgeToken) return;
    try {
      await localRequest('/event', bridgeToken, message);
    } catch (error) {
      if (!terminalEvents.includes(message.type)) {
        if (message.type === 'started') diagnose(message.id, 'relay_failed');
        throw error;
      }
      diagnose(message.id, 'relay_retry');
      try {
        await localRequest('/event', bridgeToken, message);
      } catch (retryError) {
        diagnose(message.id, 'relay_failed');
        throw retryError;
      }
    }
  });
  void eventQueue.catch(() => {});
}

async function localRequest(path, token, event, timeout = 16000) {
  const response = await fetch(bridgeUrl + path, {
    method: 'POST',
    cache: 'no-store',
    credentials: 'omit',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token, event }),
    signal: AbortSignal.timeout(timeout)
  });
  if (!response.ok) throw new Error('Desktop AI connection unavailable');
  return response.json();
}

async function connect() {
  if (polling) return;
  polling = true;
  try {
    const settings = await chrome.storage.local.get('bridgeToken');
    bridgeToken = settings.bridgeToken;
    if (!bridgeToken) return;
    while (bridgeToken) {
      const message = await localRequest('/poll', bridgeToken);
      if (message) void receive(message);
    }
  } catch {
    setTimeout(connect, 2500);
  } finally {
    polling = false;
  }
}

async function getTab(url, focus = false) {
  const { desktopAiTab } = await chrome.storage.session.get('desktopAiTab');
  let tab;
  if (desktopAiTab != null) {
    tab = await chrome.tabs.get(desktopAiTab).catch(() => null);
    if (!tab?.url?.startsWith(home) && !tab?.pendingUrl?.startsWith(home)) tab = null;
  }
  if (!tab) {
    tab = await chrome.tabs.create({ url, active: focus });
    await chrome.storage.session.set({ desktopAiTab: tab.id });
  } else if (tab.url !== url && tab.pendingUrl !== url) {
    tab = await chrome.tabs.update(tab.id, { url, active: focus || tab.active });
  } else if (focus && !tab.active) {
    tab = await chrome.tabs.update(tab.id, { active: true });
  }
  if (focus) await chrome.windows.update(tab.windowId, { focused: true });
  return tab;
}

function finish(job, message) {
  if (active !== job) return;
  clearTimeout(job.deadline);
  active = null;
  post({ id: job.id, ...message });
}

async function ask(message) {
  diagnose(message.id, 'job_received');
  if (active || stopping) {
    diagnose(message.id, 'already_busy');
    post({ type: 'error', id: message.id, text: '浏览器还在处理上一条消息。' });
    return;
  }
  const job = { id: message.id, day: message.day, tabId: null, parent: null, persistence: Promise.resolve() };
  active = job;
  try {
    const { dailySession } = await chrome.storage.local.get('dailySession');
    const url = dailySession?.day === job.day && sessionUrl.test(dailySession.url)
      ? dailySession.url : home;
    const tab = await getTab(url);
    if (active !== job || job.stopping) return;
    job.tabId = tab.id;

    let state;
    for (let attempt = 0; attempt < 60 && active === job && !job.stopping; attempt += 1) {
      try { state = await chrome.tabs.sendMessage(tab.id, { type: 'probe' }); } catch { /* Loading. */ }
      if (state?.kind === 'login' || state?.kind === 'ready' && state.url === url) break;
      await sleep(300);
    }
    if (active !== job || job.stopping) return;
    if (state?.kind === 'login') {
      diagnose(job.id, 'page_login');
      finish(job, { type: 'login', text: 'DeepSeek 需要重新登录。' });
      return;
    }
    if (state?.kind !== 'ready' || state.url !== url) {
      diagnose(job.id, 'page_unavailable');
      finish(job, { type: 'error', text: '未找到 DeepSeek 输入区，请在 Edge 查看页面状态。' });
      return;
    }
    diagnose(job.id, 'page_ready');
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ['stream.js'], world: 'MAIN' });
    if (active !== job || job.stopping) return;
    diagnose(job.id, 'hook_ready');
    const result = await chrome.tabs.sendMessage(tab.id, message);
    if (active !== job || job.stopping) return;
    if (!result?.ok) {
      diagnose(job.id, 'composer_rejected');
      finish(job, { type: result?.login ? 'login' : 'error', text: result?.text || '网页未能发送消息。' });
      return;
    }
    diagnose(job.id, 'composer_accepted');
    job.deadline = setTimeout(() => { void stop(job, true); }, 300000);
  } catch {
    if (job.stopping) return;
    diagnose(job.id, 'send_failed');
    finish(job, { type: 'error', text: '无法连接 DeepSeek 网页，请确认 Edge 和扩展已打开。' });
  }
}

async function stopTab(job) {
  if (job.tabId == null) return true;
  const confirmed = await chrome.tabs.sendMessage(job.tabId, { type: 'stop', id: job.id }).catch(() => false);
  if (active === job) {
    const tab = await chrome.tabs.get(job.tabId).catch(() => null);
    if (active === job) await saveSession(job, tab?.url || '');
  }
  return confirmed === true;
}

async function stop(job, timedOut = false) {
  if (active !== job || job.stopping) return;
  if (timedOut) diagnose(job.id, 'reply_timeout');
  stopping = job;
  job.stopping = true;
  const confirmed = await Promise.race([stopTab(job), sleep(2000).then(() => false)]);
  if (active === job) {
    finish(job, timedOut
      ? { type: 'error', text: '等待网页回答超时，请在 Edge 查看。' }
      : { type: 'stopped', confirmed });
  }
  if (stopping === job) stopping = null;
}

async function openPage() {
  const { desktopAiTab } = await chrome.storage.session.get('desktopAiTab');
  const tab = desktopAiTab == null ? null : await chrome.tabs.get(desktopAiTab).catch(() => null);
  if (tab?.url?.startsWith(home)) {
    await chrome.tabs.update(tab.id, { active: true });
    await chrome.windows.update(tab.windowId, { focused: true });
  } else {
    await getTab(home, true);
  }
}

async function receive(message) {
  switch (message?.type) {
    case 'ask':
      if (typeof message.id === 'string' && typeof message.prompt === 'string' &&
          /^\d{4}-\d{2}-\d{2}$/.test(message.day)) await ask(message);
      break;
    case 'stop':
      if (typeof message.id !== 'string') break;
      if (active?.id === message.id) await stop(active);
      else post({ type: 'stopped', id: message.id, confirmed: false });
      break;
    case 'open':
      await openPage().catch(() => {});
      break;
  }
}

async function saveSession(job, url) {
  if (!sessionUrl.test(url)) return;
  job.persistence = job.persistence.then(() =>
    chrome.storage.local.set({ dailySession: { day: job.day, url, parent: job.parent } }))
    .catch(() => diagnose(job.id, 'session_save_failed'));
  await job.persistence;
}

chrome.runtime.onMessage.addListener((event, sender, reply) => {
  if (sender.id === chrome.runtime.id && sender.url === chrome.runtime.getURL('pair.html') && event?.type === 'pair') {
    void connect();
    reply(true);
    return;
  }
  const job = active;
  if (!job || sender.tab?.id !== job.tabId || sender.frameId !== 0 || event?.id !== job.id) return;
  if (!['started', 'session', 'delta', 'complete', 'stopped', 'login', 'error'].includes(event.type)) return;
  void (async () => {
    if (event.parent != null) job.parent = event.parent;
    if (event.type === 'session') {
      await saveSession(job, event.url);
    } else if (event.type === 'complete' || event.type === 'stopped') {
      const tab = await chrome.tabs.get(job.tabId).catch(() => null);
      await saveSession(job, sessionUrl.test(event.url) ? event.url : tab?.url || '');
      finish(job, { type: job.stopping ? 'stopped' : event.type,
        answer: event.answer, thought: event.thought, search: event.search });
    } else if (event.type === 'error' || event.type === 'login') {
      if (['no_stream', 'stream_login', 'stream_no_reply', 'stream_interrupted'].includes(event.diagnostic)) {
        diagnose(job.id, event.diagnostic, event.http_status);
      }
      finish(job, { type: event.type, text: event.text });
    } else {
      post(event);
    }
  })().then(() => reply(true), () => reply(false));
  return true;
});

chrome.tabs.onRemoved.addListener(tabId => {
  if (active?.tabId === tabId) {
    diagnose(active.id, 'tab_closed');
    finish(active, { type: 'error', text: 'DeepSeek 标签页已关闭。' });
  }
});
chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === reconnectAlarm) void connect(); });
chrome.runtime.onStartup.addListener(() => { void connect(); });
chrome.runtime.onInstalled.addListener(() => { void connect(); });
void chrome.alarms.create(reconnectAlarm, { periodInMinutes: 0.5 });
void connect();
