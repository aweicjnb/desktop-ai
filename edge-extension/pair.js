'use strict';

const form = document.querySelector('form');
const code = document.getElementById('token');
const status = document.getElementById('status');
const button = form.querySelector('button');

async function ping(token) {
  const result = await fetch('http://127.0.0.1:41692/ping', {
    method: 'POST', cache: 'no-store', credentials: 'omit',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token }),
    signal: AbortSignal.timeout(3500)
  });
  if (!result.ok) throw new Error('Invalid connection');
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  button.disabled = true;
  status.textContent = '正在连接…';
  const token = code.value.trim();
  try {
    await ping(token);
    await chrome.storage.local.set({ bridgeToken: token });
    await chrome.runtime.sendMessage({ type: 'pair' }).catch(() => {});
    code.value = '';
    status.textContent = '已连接，可以回到桌面弹窗。';
  } catch {
    status.textContent = '连接失败。请确认桌面程序正在运行，并重新复制连接码。';
  } finally {
    button.disabled = false;
  }
});

void chrome.storage.local.get('bridgeToken').then(async ({ bridgeToken }) => {
  if (!bridgeToken) return;
  try {
    await ping(bridgeToken);
    status.textContent = '已连接，可以回到桌面弹窗。';
  } catch {
    status.textContent = '桌面程序当前未连接。';
  }
});
