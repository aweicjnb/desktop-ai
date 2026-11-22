'use strict';

const origin = 'https://chat.deepseek.com';
const home = origin + '/';
const bridgeUrl = 'http://127.0.0.1:41692';
const sessionUrl = /^https:\/\/chat\.deepseek\.com\/a\/chat\/s\/[a-f0-9-]{36}$/i;
const reconnectAlarm = 'desktop-ai-reconnect';
const sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
let bridgeToken;
let polling = false;
let eventQueue = Promise.resolve();
let active;
let stopping;

function post(message) {
  eventQueue = eventQueue.catch(() => {}).then(async () => {
    if (!bridgeToken) return;
    await localRequest('/event', bridgeToken, message);
  });
  void eventQueue.catch(() => {});
}

async function localRequest(path, token, event) {
  const response = await fetch(bridgeUrl + path, {
    method: 'POST',
    cache: 'no-store',
    credentials: 'omit',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token, event }),
    signal: AbortSignal.timeout(16000)
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
  if (active || stopping) {
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
    if (active !== job) return;
    job.tabId = tab.id;

    let state;
    for (let attempt = 0; attempt < 60 && active === job; attempt += 1) {
      try { state = await chrome.tabs.sendMessage(tab.id, { type: 'probe' }); } catch { /* Loading. */ }
      if (state?.kind === 'login' || state?.kind === 'ready' && state.url === url) break;
      await sleep(300);
    }
    if (active !== job) return;
    if (state?.kind === 'login') {
      finish(job, { type: 'login', text: 'DeepSeek 需要重新登录。' });
      return;
    }
    if (state?.kind !== 'ready' || state.url !== url) {
      finish(job, { type: 'error', text: '未找到 DeepSeek 输入区，请在 Edge 查看页面状态。' });
      return;
    }
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ['stream.js'], world: 'MAIN' });
    if (active !== job) return;
    const result = await chrome.tabs.sendMessage(tab.id, message);
    if (active !== job) return;
    if (!result?.ok) {
      finish(job, { type: result?.login ? 'login' : 'error', text: result?.text || '网页未能发送消息。' });
      return;
    }
    job.deadline = setTimeout(() => { void stop(job, true); }, 300000);
  } catch {
    finish(job, { type: 'error', text: '无法连接 DeepSeek 网页，请确认 Edge 和扩展已打开。' });
  }
}

async function stop(job, timedOut = false) {
  if (active !== job) return;
  stopping = job;
  job.stopping = true;
  try {
    if (job.tabId != null) await chrome.tabs.sendMessage(job.tabId, { type: 'stop', id: job.id });
  } catch { /* The tab may already have closed. */ }
  if (active === job) {
    const tab = job.tabId == null ? null : await chrome.tabs.get(job.tabId).catch(() => null);
    await saveSession(job, tab?.url || '').catch(() => {});
    finish(job, timedOut
      ? { type: 'error', text: '等待网页回答超时，请在 Edge 查看。' }
      : { type: 'stopped' });
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
      if (active && active.id === message.id) await stop(active);
      break;
    case 'open':
      await openPage().catch(() => {});
      break;
  }
}

async function saveSession(job, url) {
  if (!sessionUrl.test(url)) return;
  job.persistence = job.persistence.catch(() => {}).then(() =>
    chrome.storage.local.set({ dailySession: { day: job.day, url, parent: job.parent } }));
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
      finish(job, { type: job.stopping ? 'stopped' : event.type });
    } else if (event.type === 'error' || event.type === 'login') {
      finish(job, { type: event.type, text: event.text });
    } else {
      post(event);
    }
  })().then(() => reply(true), () => reply(false));
  return true;
});

chrome.tabs.onRemoved.addListener(tabId => {
  if (active?.tabId === tabId) {
    finish(active, { type: 'error', text: 'DeepSeek 标签页已关闭。' });
  }
});
chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === reconnectAlarm) void connect(); });
chrome.runtime.onStartup.addListener(() => { void connect(); });
chrome.runtime.onInstalled.addListener(() => { void connect(); });
void chrome.alarms.create(reconnectAlarm, { periodInMinutes: 0.5 });
void connect();
