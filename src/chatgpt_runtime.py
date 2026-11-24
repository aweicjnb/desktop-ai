"""Short-lived, restricted helper for the website's normal client checks."""
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import threading

from chatgpt_account import ChatGPTError
from diagnostics import record


PROJECT = Path(__file__).resolve().parents[1]
RUNTIME = PROJECT / '.runtime/chatgpt'
ASSETS = PROJECT / 'assets'


def ready():
    return all((RUNTIME / name).is_file() for name in ('node.exe', 'client-module.js', 'node_modules/jsdom/lib/api.js'))


def ensure_ready():
    if not ready():
        import importlib.util
        spec = importlib.util.spec_from_file_location('setup_chatgpt_runtime', PROJECT / 'scripts/setup_chatgpt_runtime.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        try:
            module.prepare()
        except Exception:
            raise ChatGPTError('ChatGPT 运行组件准备失败，请检查网络后重新连接账号。') from None
    expected = (ASSETS / 'chatgpt-runtime/module.sha256').read_text().strip()
    if sha256((RUNTIME / 'client-module.js').read_bytes()).hexdigest() != expected:
        raise ChatGPTError('ChatGPT 校验组件不匹配，请更新应用后重试。')


def solve(account, job, prepare):
    if not ready():
        raise ChatGPTError('ChatGPT 运行组件尚未准备好，请在「模型与账号」中重新连接。')
    if job['cancel'].is_set():
        raise ChatGPTError('已停止。')
    environment = {key: os.environ[key] for key in ('SystemRoot', 'WINDIR') if key in os.environ}
    environment.update(TEMP=str(RUNTIME), TMP=str(RUNTIME))
    process = subprocess.Popen([
        str(RUNTIME / 'node.exe'), '--permission', '--allow-fs-read=' + str(RUNTIME),
        '--allow-fs-read=' + str(ASSETS), str(ASSETS / 'chatgpt-check.mjs'), str(RUNTIME),
    ], cwd=RUNTIME, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
       stderr=subprocess.DEVNULL, text=True, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW)
    job['process'] = process
    timer = threading.Timer(65, lambda: process.kill() if process.poll() is None else None)
    timer.daemon = True
    timer.start()
    def send(value):
        process.stdin.write(json.dumps(value) + '\n')
        process.stdin.flush()
    try:
        send({'user_agent': account['user_agent']})
        for line in process.stdout:
            if job['cancel'].is_set():
                raise ChatGPTError('已停止。')
            if len(line) > 4 * 1024 * 1024:
                raise ChatGPTError('ChatGPT 校验返回异常，请更新应用后重试。')
            event = json.loads(line)
            if event['type'] == 'prepare':
                requirements = prepare(event['p'])
                send({'requirements': requirements})
            elif event['type'] == 'integrity':
                record('chatgpt.web_check_completed', job['id'],
                       proof=event['proof_present'], environment=event['environment_present'])
                return event['headers'], requirements
            elif event['type'] == 'error':
                raise ChatGPTError('ChatGPT 网页校验未完成，请重试或更新应用。')
        raise ChatGPTError('ChatGPT 校验进程意外结束，请重试。')
    finally:
        timer.cancel()
        process.stdin.close()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        process.stdout.close()
        if job.get('process') is process:
            job['process'] = None
