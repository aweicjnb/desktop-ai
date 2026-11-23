'use strict';

const origin = 'https://chat.deepseek.com';
const home = origin + '/';
const bridgeUrl = 'http://127.0.0.1:41692';
const sessionUrl = /^https:\/\/chat\.deepseek\.com\/a\/chat\/s\/[a-f0-9-]{36}$/i;
const reconnectAlarm = 'desktop-ai-reconnect';
const startTimeout = 12000;
const terminalEvents = ['complete', 'stopped', 'login', 'error'];
const sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
let bridgeToken;
let polling = false;
let eventQueue = Promise.resolve();
let tabSelection = Promise.resolve();
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
      const message = await localRequest('/poll', bridgeToken, { claim: true });
      if (!message) continue;
      const { claimRequired, ...job } = message;
      if (claimRequired) {
        const claimed = await localRequest('/claim', bridgeToken, { id: job.id }, 3000);
        if (!claimed) continue;
      }
      void receive(job);
    }
  } catch {
    setTimeout(connect, 2500);
  } finally {
    polling = false;
  }
}

function tabUrl(tab) {
  return (tab?.pendingUrl || tab?.url || '').split(/[?#]/, 1)[0];
}

function getTab(options) {
  const selected = tabSelection.then(() => selectTab(options));
  tabSelection = selected.catch(() => {});
  return selected;
}

async function selectTab({ day, focus = false }) {
  const { dailySession, desktopAiTabRef } = await chrome.storage.local.get(['dailySession', 'desktopAiTabRef']);
  const lastUrl = sessionUrl.test(dailySession?.url) ? dailySession.url : null;
  let tab = null;
  if (Number.isInteger(desktopAiTabRef?.id)) {
    const candidate = await chrome.tabs.get(desktopAiTabRef.id).catch(() => null);
    const url = tabUrl(candidate);
    // A stored ID can outlive its browser session. Check the page as well.
    if (url.startsWith(home) && (candidate.id === active?.tabId || url === desktopAiTabRef.url || url === lastUrl ||
        /^\/(?:login|sign_?in|sign_?up|register)(?:\/|$)/i.test(url.slice(origin.length)))) {
      tab = candidate;
    }
  }
  if (!tab && lastUrl) {
    const candidates = await chrome.tabs.query({ url: lastUrl + '*' });
    tab = candidates.filter(candidate => tabUrl(candidate) === lastUrl)
      .sort((first, second) => Number(Boolean(first.discarded)) - Number(Boolean(second.discarded)) ||
        second.id - first.id)[0];
  }

  const url = day ? (dailySession?.day === day && lastUrl) || home : tabUrl(tab) || lastUrl || home;
  if (!tab) {
    tab = await chrome.tabs.create({ url, active: focus });
  } else if (tabUrl(tab) !== url || (!focus && (tab.discarded || tab.frozen))) {
    tab = await chrome.tabs.update(tab.id, { url, active: focus || tab.active });
  } else if (focus && !tab.active) {
    tab = await chrome.tabs.update(tab.id, { active: true });
  }
  const ref = { id: tab.id, url: tabUrl(tab) || url };
  if (ref.id !== desktopAiTabRef?.id || ref.url !== desktopAiTabRef?.url) {
    await chrome.storage.local.set({ desktopAiTabRef: ref }).catch(() => {});
  }
  if (focus) await chrome.windows.update(tab.windowId, { focused: true });
  return { tab, url };
}

function finish(job, message) {
  if (active !== job) return;
  clearTimeout(job.startDeadline);
  clearTimeout(job.deadline);
  active = null;
  post({ id: job.id, ...message });
}

function acknowledgeStart(job, notify = true) {
  if (job.started) return;
  job.started = true;
  clearTimeout(job.startDeadline);
  if (notify) post({ type: 'started', id: job.id });
}

async function verifyStart(job) {
  if (active !== job || job.started || job.stopping) return;
  // This timer runs in the extension, independently of timers on a hidden page.
  const result = await Promise.race([
    chrome.tabs.sendMessage(job.tabId, { type: 'verify-start', id: job.id }).catch(() => null),
    sleep(1500).then(() => null)
  ]);
  if (active !== job || job.started || job.stopping) return;
  if (result?.type === 'started') {
    acknowledgeStart(job);
    return;
  }
  diagnose(job.id, 'no_stream');
  finish(job, { type: result?.type === 'login' ? 'login' : 'error',
    text: result?.text || 'DeepSeek 网页未确认发送。问题已保留，请先查看网页再重试。' });
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
    const { tab, url } = await getTab({ day: job.day });
    if (active !== job || job.stopping) return;
    job.tabId = tab.id;

    let state, injectedContent = false;
    for (let attempt = 0; attempt < 60 && active === job && !job.stopping; attempt += 1) {
      try {
        state = await chrome.tabs.sendMessage(tab.id, { type: 'probe' });
      } catch {
        if (!injectedContent && active === job && !job.stopping) {
          // Pages left open during an extension reload can lose their content script.
          injectedContent = await chrome.scripting.executeScript({
            target: { tabId: tab.id }, files: ['content.js']
          }).then(() => true, () => false);
          if (injectedContent) continue;
        }
      }
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
    const result = await chrome.tabs.sendMessage(tab.id, { ...message, type: 'ask' });
    if (active !== job || job.stopping) return;
    if (!result?.ok) {
      diagnose(job.id, result?.diagnostic === 'image_upload_failed' ? 'image_upload_failed' : 'composer_rejected');
      finish(job, { type: result?.login ? 'login' : 'error', text: result?.text || '网页未能发送消息。' });
      return;
    }
    diagnose(job.id, 'composer_accepted');
    if (!job.started) job.startDeadline = setTimeout(() => { void verifyStart(job); }, startTimeout);
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

async function receive(message) {
  switch (message?.type) {
    case 'ask':
    case 'ask_image':
      if (typeof message.id === 'string' && typeof message.prompt === 'string' &&
          /^\d{4}-\d{2}-\d{2}$/.test(message.day)) {
        if (message.type === 'ask_image' && !message.image) {
          post({ type: 'error', id: message.id, text: '没有收到图片数据，请重新粘贴后发送。' });
        } else {
          await ask(message);
        }
      }
      break;
    case 'stop':
      if (typeof message.id !== 'string') break;
      if (active?.id === message.id) await stop(active);
      else post({ type: 'stopped', id: message.id, confirmed: false });
      break;
    case 'open':
      await getTab({ focus: true }).catch(() => {});
      break;
  }
}

async function saveSession(job, url) {
  if (!sessionUrl.test(url)) return;
  job.persistence = job.persistence.then(() =>
    chrome.storage.local.set({
      dailySession: { day: job.day, url, parent: job.parent },
      desktopAiTabRef: { id: job.tabId, url }
    }))
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
    if (['started', 'session', 'delta', 'complete'].includes(event.type)) {
      acknowledgeStart(job, event.type === 'started' || event.type === 'session');
    }
    if (event.parent != null) job.parent = event.parent;
    if (event.type === 'started') return;
    if (event.type === 'session') {
      await saveSession(job, event.url);
    } else if (event.type === 'complete' || event.type === 'stopped') {
      const tab = await chrome.tabs.get(job.tabId).catch(() => null);
      await saveSession(job, sessionUrl.test(event.url) ? event.url : tab?.url || '');
      finish(job, { type: job.stopping ? 'stopped' : event.type,
        answer: event.answer, thought: event.thought, search: event.search });
    } else if (event.type === 'error' || event.type === 'login') {
      if (['no_stream', 'stream_login', 'stream_no_reply', 'stream_interrupted', 'stream_image_missing'].includes(event.diagnostic)) {
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
