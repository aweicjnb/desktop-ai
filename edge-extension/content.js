'use strict';

(() => {
  const emit = (name, detail) => window.dispatchEvent(new CustomEvent('desktop-ai:' + name, { detail }));
  const composerSelector = 'textarea[placeholder*="DeepSeek"], textarea[aria-label*="DeepSeek"]';
  const sendSelector = '._52c986b, [aria-label="Send"], [aria-label="发送"]';
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  let pending;

  function editor() {
    return [...document.querySelectorAll(composerSelector)].find(item => item.getClientRects().length && !item.disabled);
  }

  function composerButton(input) {
    return input?.closest('.ds-textarea')?.querySelector(sendSelector);
  }

  function probe() {
    const url = location.origin + location.pathname;
    if (/\/(?:login|sign_?in|sign_?up|register)(?:\/|$)/i.test(location.pathname) ||
        (!editor() && document.querySelector('input[type="password"]'))) return { kind: 'login', url };
    return { kind: editor() ? 'ready' : 'loading', url };
  }

  function relay(event) {
    void chrome.runtime.sendMessage(event).catch(() => {});
  }

  function toggle(names, desired) {
    const button = [...document.querySelectorAll('.ds-toggle-button[aria-pressed]')].find(item =>
      item.getClientRects().length && names.includes(item.textContent.trim()));
    if (!button) throw new Error('网页的搜索或思考按钮已变化，请更新扩展后重试。');
    if ((button.getAttribute('aria-pressed') === 'true') !== desired) button.click();
  }

  async function ask(message) {
    const input = editor();
    if (!input) return { ok: false, login: probe().kind === 'login', text: 'DeepSeek 输入区暂不可用。' };
    if (pending || input.value.trim()) {
      return { ok: false, text: 'DeepSeek 标签页有尚未处理的输入或请求，请在 Edge 检查。' };
    }
    const job = { id: message.id, input };
    pending = job;
    try {
      toggle(['深度思考', 'DeepThink', 'DeepThink (R1)', 'Deep thinking', 'Thinking'], message.thinking === true);
      toggle(['智能搜索', '联网搜索', 'Search', 'Web Search', 'Smart Search'], message.search === true);
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(input, message.prompt);
      input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: message.prompt }));
      await sleep(40);
      if (pending !== job) return { ok: false, text: '已停止。' };
      if (!input.isConnected || input.value !== message.prompt) throw new Error('网页没有接受输入，请检查 DeepSeek 标签页。');

      emit('arm', { id: message.id, prompt: message.prompt });
      const button = composerButton(input);
      if (button && button.getAttribute('aria-disabled') !== 'true' && !button.disabled) {
        button.click();
      } else {
        input.dispatchEvent(new KeyboardEvent('keydown', {
          key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true
        }));
      }
      if (!job.seenStream) job.timer = setTimeout(() => {
        if (pending !== job) return;
        pending = null;
        emit('disarm', { id: job.id });
        const needsLogin = probe().kind === 'login' || /请完成真人验证|verify you.re human/i.test(document.body.innerText);
        relay({ type: needsLogin ? 'login' : 'error', id: job.id,
          text: needsLogin ? '请在 Edge 完成登录或验证。' : '网页没有开始回答，请在 Edge 查看后再试。' });
      }, 25000);
      return { ok: true };
    } catch (error) {
      pending = null;
      emit('disarm', { id: job.id });
      return { ok: false, text: error.message || '未能使用 DeepSeek 网页输入框。' };
    }
  }

  window.addEventListener('desktop-ai:stream', ({ detail }) => {
    const job = pending;
    if (!job || detail?.id !== job.id) return;
    if (!['started', 'session', 'delta', 'complete', 'stopped', 'login', 'error'].includes(detail.type)) return;
    clearTimeout(job.timer);
    job.seenStream = true;
    if (['complete', 'stopped', 'login', 'error'].includes(detail.type)) pending = null;
    relay(detail);
  });

  chrome.runtime.onMessage.addListener((message, sender, reply) => {
    if (sender.id !== chrome.runtime.id) return;
    if (message.type === 'probe') {
      reply(probe());
    } else if (message.type === 'ask') {
      void ask(message).then(reply);
      return true;
    } else if (message.type === 'stop') {
      if (pending?.id === message.id) {
        const job = pending;
        pending = null;
        clearTimeout(job.timer);
        const input = editor();
        if (job.input === input && !input.value.trim()) composerButton(input)?.click();
        emit('abort', { id: message.id });
      }
      reply(true);
    }
  });
})();
