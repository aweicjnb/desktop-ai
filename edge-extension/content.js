'use strict';

(() => {
  if (globalThis.__desktopAiContentInstalled) return;
  globalThis.__desktopAiContentInstalled = true;

  const emit = (name, detail) => window.dispatchEvent(new CustomEvent('desktop-ai:' + name, { detail }));
  const composerSelector = 'textarea[placeholder*="DeepSeek"], textarea[aria-label*="DeepSeek"]';
  const sendSelector = '._52c986b, [aria-label="Send"], [aria-label="发送"]';
  const disabledControlSelector = ':disabled, [aria-disabled="true"], .ds-button--disabled, .ds-button--loading, .bd74640a';
  const attachmentSelector = '.d5fa3d1b, ._25c7358';
  const attachmentErrorSelector = '.b909beb4, .f90eff51, ._6e3a316';
  const removeAttachmentSelector = '.c8b3f8a6, ._8402d8c';
  const maxImageBytes = 8 * 1024 * 1024;
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  let pending;

  function editor() {
    return [...document.querySelectorAll(composerSelector)].find(item => item.getClientRects().length && !item.disabled);
  }

  function composer(input) {
    return input?.closest('._77cefa5, .ds-textarea');
  }

  function enabled(control) {
    return control && !control.matches(disabledControlSelector);
  }

  function imageCard(job) {
    if (!job.imageName) return null;
    return [...job.composer.querySelectorAll(attachmentSelector)].find(card =>
      [...card.querySelectorAll('img[alt]')].some(image => image.alt === job.imageName) ||
      card.textContent.includes(job.imageName));
  }

  function uploadImage(job, data) {
    const picker = job.composer?.querySelector('input[type="file"]');
    if (!enabled(picker)) throw new Error('当前 DeepSeek 页面暂时无法上传图片，请在 Edge 检查。');
    if (typeof data !== 'string' || !data.startsWith('iVBORw0KGgo') || data.length > 4 * Math.ceil(maxImageBytes / 3)) {
      throw new Error('收到的图片格式或大小有误，请重新粘贴。');
    }
    const binary = atob(data);
    if (binary.length > maxImageBytes) throw new Error('单张图片最多 8 MiB。');
    const bytes = Uint8Array.from(binary, byte => byte.charCodeAt(0));
    job.imageName = `desktop-ai-${job.id.slice(0, 12)}.png`;
    const transfer = new DataTransfer();
    transfer.items.add(new File([bytes], job.imageName, { type: 'image/png' }));
    picker.files = transfer.files;
    picker.dispatchEvent(new Event('change', { bubbles: true }));
  }

  async function waitForImage(job) {
    const deadline = performance.now() + 45000;
    while (pending === job && performance.now() < deadline) {
      if (!job.input.isConnected || job.input.value !== job.prompt) {
        throw new Error('图片上传期间网页输入框已变化，请重新发送。');
      }
      const card = imageCard(job);
      if (card?.matches(attachmentErrorSelector) || card?.querySelector(attachmentErrorSelector)) {
        throw new Error('DeepSeek 未能处理这张图片，请在 Edge 查看。');
      }
      const button = job.composer.querySelector(sendSelector);
      if (card && enabled(button)) return button;
      await sleep(150);
    }
    if (pending === job) throw new Error('等待 DeepSeek 图片上传超时，请重新发送。');
  }

  async function waitForSendButton(job) {
    const deadline = performance.now() + 4000;
    while (pending === job && performance.now() < deadline) {
      if (!job.input.isConnected || job.input.value !== job.prompt) {
        throw new Error('网页输入框已变化，请重新发送。');
      }
      const button = job.composer?.querySelector(sendSelector);
      if (enabled(button)) return button;
      await sleep(150);
    }
    if (pending === job) throw new Error('DeepSeek 的发送按钮暂未就绪。问题已保留，请重试。');
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

  function setEditorValue(input, value) {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(input, value);
    input.dispatchEvent(new InputEvent('input', { bubbles: true,
      inputType: value ? 'insertText' : 'deleteContentBackward', data: value || null }));
  }

  function clearPreparedDraft(job) {
    if (job.wrotePrompt && job.input.isConnected && job.input.value === job.prompt) setEditorValue(job.input, '');
    imageCard(job)?.querySelector(removeAttachmentSelector)?.click();
  }

  function expireUnconfirmedSend(job) {
    if (pending !== job || job.seenStream) return null;
    pending = null;
    clearTimeout(job.timer);
    emit('disarm', { id: job.id });
    clearPreparedDraft(job);
    const needsLogin = probe().kind === 'login' || /请完成真人验证|verify you.re human/i.test(document.body?.innerText || '');
    return { type: needsLogin ? 'login' : 'error', id: job.id, diagnostic: 'no_stream',
      text: needsLogin ? '请在 Edge 完成登录或验证。' : 'DeepSeek 网页未确认发送。问题已保留，请先查看网页再重试。' };
  }

  async function ask(message) {
    const input = editor();
    if (!input) return { ok: false, login: probe().kind === 'login', text: 'DeepSeek 输入区暂不可用。' };
    const scope = composer(input);
    if (pending || input.value.trim() || scope?.querySelector(attachmentSelector)) {
      return { ok: false, text: 'DeepSeek 标签页有尚未处理的输入或请求，请在 Edge 检查。' };
    }
    const job = { id: message.id, input, composer: scope, prompt: message.prompt, submitted: false };
    pending = job;
    try {
      toggle(['深度思考', 'DeepThink', 'DeepThink (R1)', 'Deep thinking', 'Thinking'], message.thinking === true);
      toggle(['智能搜索', '联网搜索', 'Search', 'Web Search', 'Smart Search'], !message.image && message.search === true);
      if (message.image) {
        await sleep(40);
        if (pending !== job) return { ok: false, text: '已停止。' };
        if (input.value.trim()) throw new Error('网页输入框已有文字，请在 Edge 检查。');
        uploadImage(job, message.image);
      }
      job.wrotePrompt = true;
      setEditorValue(input, job.prompt);
      await sleep(40);
      if (pending !== job) return { ok: false, text: '已停止。' };
      if (!input.isConnected || input.value !== message.prompt) throw new Error('网页没有接受输入，请检查 DeepSeek 标签页。');

      const button = message.image ? await waitForImage(job) : await waitForSendButton(job);
      if (pending !== job) return { ok: false, text: '已停止。' };
      emit('arm', { id: message.id, prompt: message.prompt, image: Boolean(message.image) });
      job.submitted = true;
      button.click();
      if (!job.seenStream) job.timer = setTimeout(() => {
        const error = expireUnconfirmedSend(job);
        if (error) relay(error);
      }, 25000);
      return { ok: true };
    } catch (error) {
      if (pending !== job) return { ok: false, text: '已停止。' };
      pending = null;
      emit('disarm', { id: job.id });
      if (!job.submitted) clearPreparedDraft(job);
      return { ok: false, text: error.message || '未能使用 DeepSeek 网页输入框。',
        diagnostic: message.image ? 'image_upload_failed' : undefined };
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
    } else if (message.type === 'verify-start') {
      const job = pending;
      reply(job?.id === message.id ? (job.seenStream ? { type: 'started' } : expireUnconfirmedSend(job)) : null);
    } else if (message.type === 'stop') {
      if (pending?.id !== message.id) {
        reply(false);
        return;
      }
      const job = pending;
      pending = null;
      clearTimeout(job.timer);
      const input = editor();
      if (!job.submitted) clearPreparedDraft(job);
      else if (job.input === input && !input.value.trim()) job.composer?.querySelector(sendSelector)?.click();
      emit('abort', { id: message.id });
      reply(true);
    }
  });
})();
