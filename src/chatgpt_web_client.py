"""Website HTTPS transport using the user's imported Chrome session."""
from base64 import b64decode
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from hashlib import sha256
from http.cookies import SimpleCookie
import json
from queue import Queue
import re
import socket
import threading
import time
import traceback
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener
import uuid

from chatgpt_account import ChatGPTError, ChatGPTLoginRequired, NoRedirect, account_lock, write_protected
from chatgpt_client import ChatGPTClient as QueueClient, Cancelled
from chatgpt_runtime import ensure_ready, solve
from chatgpt_web_account import (
    load_account, load_store, normalize, save_account, session_cookies, store_path, update_registration,
)
from chatgpt_web_stream import WebStream, events
from clipboard_image import MAX_IMAGE_BYTES, ClipboardImageError, read_png
from diagnostics import record
from http_transport import HTTPSPool, JobHTTPS, abort_io
from prepared_check import PreparedCheck, PreparedCheckCache, positive_number
from rich_content import has_asset


class CheckRejected(ChatGPTError):
    pass


def error_description(status, body, stage):
    try:
        value = json.loads(body)
    except ValueError:
        value = {}
    error = value.get('error') if isinstance(value, dict) else None
    code = error.get('code') if isinstance(error, dict) else error
    code = code if isinstance(code, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', code) else 'unknown'
    record('chatgpt.web_http_error', http_status=status, phase=stage, code=code)
    if status == 401 or code in ('token_expired', 'invalid_token', 'session_expired'):
        return ChatGPTLoginRequired('ChatGPT 网页登录需要更新，请在「模型与账号」中重新导入网页会话。')
    if status == 429:
        return ChatGPTError('ChatGPT 当前额度或请求频率已达限制，请稍后重试。')
    if status == 403:
        return CheckRejected('ChatGPT 未接受本次网页校验，请稍后重试；持续出现时请更新应用或重新导入会话。')
    if status == 404 and stage == 'history':
        return ChatGPTError('ChatGPT 当前会话已不存在，请在设置中开始新会话。')
    if status >= 500:
        return ChatGPTError(f'ChatGPT 服务暂时不可用，请稍后重试。（HTTP {status}）')
    return ChatGPTError(f'ChatGPT 请求失败（{stage}，HTTP {status}，{code}）。')


class ChatGPTWebClient(QueueClient):
    def __init__(self, account=None, model=None):
        self.account = normalize(account) if account is not None else load_account()
        self.client_id = self.account['client_id']
        self.models = self.account.get('models', [])
        self.model = model or self.account.get('model') or 'auto'
        self.events = Queue()
        self.lock = threading.Lock()
        self.active_request = self.active = None
        self.connected = True
        self.http = HTTPSPool()
        self.checks = PreparedCheckCache(self.prepare_check, self.check_identity)

    def check_identity(self):
        account = self.account
        values = [self.client_id] + [account.get(key, '') for key in ('device_id', 'user_agent', 'access_token', 'cookie_header')]
        return sha256(json.dumps(values).encode('utf-8')).digest()

    def start_preparation(self):
        self.checks.start()

    def prepare_check(self, job):
        self.refresh_account()
        identity = self.check_identity()
        headers = self.integrity(job)
        return PreparedCheck(headers, identity, time.monotonic(), time.time(),
                             job.get('check_expires_at'), job.get('check_expires_after'))

    def take_integrity(self, job, force_new=False):
        if self.checks.enabled:
            self.emit(job, 'progress', text='正在准备连接')
            job['_used_prefetched_check'] = True
            return self.checks.take(job, force_new)
        return self.integrity(job)

    def request(self, path, body=None, headers=None, job=None, stage='request', refresh=True):
        started = time.monotonic()
        if not path.startswith(('/backend-api/', '/api/auth/')):
            raise ValueError('Unexpected ChatGPT path')
        if job and job['cancel'].is_set():
            raise Cancelled()
        account = self.account
        options = {'Authorization': 'Bearer ' + account['access_token'],
                   'Cookie': account.get('cookie_header', ''),
                   'User-Agent': account['user_agent'], 'Oai-Device-Id': account['device_id'],
                   'Origin': 'https://chatgpt.com', 'Referer': 'https://chatgpt.com/',
                   'Accept': 'application/json', 'Oai-Language': 'zh-CN', **(headers or {})}
        data = None
        if body is not None:
            data = json.dumps(body, separators=(',', ':')).encode('utf-8')
            options['Content-Type'] = 'application/json'
        try:
            response = build_opener(NoRedirect(), JobHTTPS(job, self.http)).open(
                Request('https://chatgpt.com' + path, data=data, headers=options), timeout=12)
            if job:
                job['response'] = response
                if job['cancel'].is_set():
                    response.close()
                    raise Cancelled()
            record('chatgpt.web_http_ready', job['id'] if job else None, phase=stage,
                   elapsed_ms=round((time.monotonic()-started)*1000), reused=response.reused)
            return response
        except HTTPError as error:
            description = error_description(error.code, error.read(128 * 1024), stage)
            error.close()
            if isinstance(description, ChatGPTLoginRequired) and refresh:
                self.refresh_account(force=True)
                return self.request(path, body, headers, job, stage, refresh=False)
            raise description from None
        except (URLError, TimeoutError, OSError):
            if job and job['cancel'].is_set():
                raise Cancelled() from None
            raise ChatGPTError(f'连接 ChatGPT 超时或中断（{stage}），请检查网络后重试。') from None

    def json_request(self, path, body=None, **kwargs):
        with self.request(path, body, **kwargs) as response:
            raw = response.read(8 * 1024 * 1024)
            value = json.loads(raw)
        if not isinstance(value, dict):
            raise ChatGPTError('ChatGPT 返回了异常数据，请更新应用后重试。')
        return value

    def refresh_account(self, force=False):
        # A renewable website cookie and the current access token are distinct.
        # Always serialize cookie rotation and persist it before honoring stop.
        with account_lock():
            try:
                latest = load_account(self.client_id)
                if latest['expires_at'] >= self.account['expires_at']:
                    self.account = latest
            except FileNotFoundError:
                pass
            if not force and self.account['expires_at'] > time.time() + 300:
                return
            if not self.account.get('cookie_header'):
                raise ChatGPTLoginRequired('ChatGPT 会话缺少续期数据，请重新导入完整的网页会话 JSON。')
            with self.request('/api/auth/session', stage='session_refresh', refresh=False) as response:
                value = json.load(response)
                cookies = response.headers.get_all('Set-Cookie') or []
            if not value.get('accessToken') or value.get('error'):
                raise ChatGPTLoginRequired('ChatGPT 网页会话续期失败，请重新导入当前 Chrome 会话。')
            candidate = normalize({**self.account, 'access_token': value['accessToken'],
                                   'sessionToken': value.get('sessionToken'), 'email': value.get('user', {}).get('email') or self.account['email']})
            if candidate['subject'] != self.account['subject'] or candidate['expires_at'] <= time.time():
                raise ChatGPTLoginRequired('ChatGPT 返回的会话与当前账号不一致或已过期，请重新导入。')
            parsed = SimpleCookie()
            for cookie in cookies:
                try:
                    parsed.load(cookie)
                except Exception:
                    continue
            parts = dict(item.strip().split('=', 1) for item in candidate['cookie_header'].split(';') if '=' in item)
            for name, item in parsed.items():
                if name.startswith(('__Secure-next-auth.session-token', 'cf_', '__cf_')):
                    if item.value:
                        parts[name] = item.value
                    else:
                        parts.pop(name, None)
            candidate['cookie_header'] = '; '.join(f'{name}={value}' for name, value in parts.items())
            value = load_store()
            old = value['registrations'].get(self.client_id, {})
            value['registrations'][self.client_id] = {**old, **candidate}
            write_protected(store_path(), value)
            self.account = candidate
            record('chatgpt.web_session_refreshed')

    def connect_account(self):
        ensure_ready()
        self.refresh_account()
        catalog = self.json_request('/backend-api/models?history_and_training_disabled=false', stage='models')
        allowed = {item.get('default_model') for item in catalog.get('categories', [])}
        for item in catalog.get('categories', []):
            allowed.update(item.get('supported_models', []))
        allowed.add('auto')
        self.models = [{**item, 'display_name': item.get('title') or item['slug']}
                       for item in catalog.get('models', []) if item.get('slug') in allowed]
        if not self.models:
            raise ChatGPTError('当前 ChatGPT 网页账号没有返回可用模型，请重新导入。')
        if self.model not in {item['slug'] for item in self.models}:
            self.model = catalog.get('default_model_slug')
            if self.model not in {item['slug'] for item in self.models}:
                self.model = 'auto' if 'auto' in allowed else self.models[0]['slug']
        self.account.update(models=self.models, model=self.model, categories=catalog.get('categories', []))
        save_account(self.account)
        record('chatgpt.web_account_connected', models=len(self.models))

    def daily_session(self, job):
        current = load_account(self.client_id)
        if current.get('session_date') == job['day'] and current.get('session_id'):
            session_id = current['session_id']
            if not re.fullmatch(r'[0-9a-fA-F-]{36}', session_id):
                raise ChatGPTError('ChatGPT 会话数据异常，请在设置中开始新会话。')
            history = self.json_request('/backend-api/conversation/' + session_id, job=job, stage='history')
            parent = history.get('current_node') or current.get('parent_id')
            if not parent:
                raise ChatGPTError('ChatGPT 会话位置无法读取，请在设置中开始新会话。')
            last = history.get('mapping', {}).get(parent, {}).get('message') or {}
            if last.get('author', {}).get('role') == 'assistant' and last.get('status') in ('in_progress', 'pending'):
                # Image-only turns can leave a hidden, empty assistant stub
                # in_progress even after the public image result is complete.
                mapping = history.get('mapping') or {}
                previous_id = (mapping.get(parent) or {}).get('parent')
                previous = (mapping.get(previous_id) or {}).get('message') or {}
                hidden_stub = ((last.get('metadata') or {}).get('is_visually_hidden_from_conversation')
                               and (last.get('content') or {}).get('content_type') == 'text'
                               and not any((last.get('content') or {}).get('parts') or []))
                finished_image = (previous.get('status') == 'finished_successfully'
                                  and previous.get('author', {}).get('role') in ('assistant', 'tool')
                                  and previous.get('channel') in (None, 'final', 'commentary')
                                  and previous.get('recipient') in (None, 'all')
                                  and not (previous.get('metadata') or {}).get('is_visually_hidden_from_conversation')
                                  and has_asset(previous.get('content') or {}))
                if not (hidden_stub and finished_image):
                    raise ChatGPTError('ChatGPT 上一条回复仍在生成，请稍后再发送。')
                parent = previous_id
            job.update(session_id=session_id, parent_id=parent)
        else:
            job.update(session_id=None, parent_id=str(uuid.uuid4()))
        return job['session_id']

    def reset_session(self):
        if self.active:
            raise ChatGPTError('请先结束当前请求。')
        update_registration(self.client_id, {'session_id': None, 'parent_id': None, 'session_date': date.today().isoformat()})

    def current_url(self):
        account = load_account(self.client_id)
        identifier = account.get('session_id')
        return 'https://chatgpt.com/c/' + identifier if identifier else 'https://chatgpt.com/'

    def load_reply_image(self, descriptor, job):
        job.setdefault('id',uuid.uuid4().hex)
        from chatgpt_media import fetch_media
        from image_codec import ImageLoadError
        from rich_content import message_content
        if descriptor.get('kind') in ('pending','unavailable') and descriptor.get('query'):
            conversation = descriptor.get('conversation_id')
            if not isinstance(conversation,str) or not re.fullmatch(r'[0-9a-fA-F-]{36}',conversation):
                raise ImageLoadError('配图暂未就绪，请稍后重试。')
            for attempt in range(3):
                if job['cancel'].is_set():
                    raise ImageLoadError('图片加载已取消。')
                history = self.json_request('/backend-api/conversation/'+conversation,job=job,stage='rich_image')
                message = ((history.get('mapping') or {}).get(descriptor.get('message_id')) or {}).get('message') or {}
                _, images = message_content(message)
                image = next((image for image in images if image['key']==descriptor['key'] and image.get('kind')=='remote'),None)
                if image:
                    return fetch_media(self,image,job)
                if attempt<2:
                    job['cancel'].wait(.6)
            raise ImageLoadError('配图暂未就绪，请稍后重试。')
        return fetch_media(self, descriptor, job)

    def integrity(self, job):
        started = time.monotonic()
        if not job.get('_background_check'):
            self.emit(job, 'progress', text='正在校验连接')
        record('chatgpt.web_check_started', job['id'])
        headers, requirements = solve(self.account, job, lambda p: self.json_request(
            '/backend-api/sentinel/chat-requirements/prepare', {'p': p}, job=job, stage='check_prepare'))
        expiry = requirements
        if requirements.get('prepare_token') and not requirements.get('token'):
            body = {'prepare_token': requirements['prepare_token']}
            if headers.get('OpenAI-Sentinel-Proof-Token'):
                body['proofofwork'] = headers['OpenAI-Sentinel-Proof-Token']
            if headers.get('OpenAI-Sentinel-Turnstile-Token'):
                body['turnstile'] = headers['OpenAI-Sentinel-Turnstile-Token']
            final = self.json_request('/backend-api/sentinel/chat-requirements/finalize', body, job=job, stage='check_finalize')
            if not final.get('token'):
                raise ChatGPTError('ChatGPT 未接受校验结果，请稍后重试。')
            headers['OpenAI-Sentinel-Chat-Requirements-Token'] = final['token']
            headers.pop('OpenAI-Sentinel-Chat-Requirements-Prepare-Token', None)
            expiry = final
        job['check_expires_at'] = positive_number(expiry.get('expire_at'))
        job['check_expires_after'] = positive_number(expiry.get('expire_after'))
        record('chatgpt.web_check_accepted', job['id'], elapsed_ms=round((time.monotonic()-started)*1000))
        return headers

    def build_payload(self, job, attachment=None):
        model = job['model']
        if job.get('thinking'):
            candidates = [item['default_model'] for item in self.account.get('categories', [])
                          if 'thinking' in item.get('model_lane', '') or 'reasoning' in item.get('category', '')]
            candidate = next((slug for slug in candidates if slug in {item['slug'] for item in self.models}), None)
            if candidate:
                model = candidate
        content = {'content_type': 'text', 'parts': [job['prompt']]}
        metadata = {}
        if attachment:
            content = {'content_type': 'multimodal_text', 'parts': [attachment['pointer'], job['prompt']]}
            metadata['attachments'] = [attachment['metadata']]
        payload = {'action': 'next', 'messages': [{'id': str(uuid.uuid4()), 'author': {'role': 'user'},
                   'content': content, 'metadata': metadata}], 'parent_message_id': job['parent_id'], 'model': model,
                   'timezone_offset_min': -int(datetime.now().astimezone().utcoffset().total_seconds() // 60),
                   'conversation_mode': {'kind': 'primary_assistant'}, 'history_and_training_disabled': False,
                   'supports_buffering': True, 'supported_encodings': ['v1'], 'client_prepare_state': 'none'}
        if job.get('session_id'):
            payload['conversation_id'] = job['session_id']
        if job.get('search'):
            payload['force_use_search'] = True
            payload['system_hints'] = ['search']
        return payload

    def upload(self, job):
        self.emit(job, 'progress', text='正在上传图片')
        try:
            data = b64decode(job['image'], validate=True)
            if len(data) > MAX_IMAGE_BYTES:
                raise ValueError()
            image = read_png(data)
        except (ValueError, OSError, ClipboardImageError):
            raise ChatGPTError('图片格式或大小不符合要求，请重新粘贴 PNG。') from None
        entry = self.json_request('/backend-api/files', {'file_name': 'clipboard.png', 'file_size': len(data),
            'use_case': 'multimodal', 'mime_type': 'image/png', 'client_resolved_mime_type': 'image/png',
            'reset_rate_limits': False, 'supports_direct_azure_multipart': False,
            'timezone_offset_min': -int(datetime.now().astimezone().utcoffset().total_seconds() // 60)},
            job=job, stage='image_create')
        file_id, url = entry.get('file_id'), entry.get('upload_url')
        host = urlsplit(url or '').hostname or ''
        if not file_id or urlsplit(url or '').scheme != 'https' or not (
                host.endswith(('.blob.core.windows.net', '.oaiusercontent.com', '.oaiusercontent.cn')) or host == 'chatgpt.com'):
            raise ChatGPTError('ChatGPT 返回了未识别的图片上传地址，请更新应用。')
        # The signed upload address gets only upload-specific headers, never
        # the ChatGPT Authorization or Cookie headers.
        headers = {'Content-Type': 'image/png', 'x-ms-blob-type': 'BlockBlob', **entry.get('upload_headers', {})}
        try:
            with build_opener(NoRedirect(), JobHTTPS(job, self.http)).open(Request(url, data=data, headers=headers, method='PUT'), timeout=12) as response:
                response.read(65536)
        except (HTTPError, URLError, OSError):
            if job['cancel'].is_set():
                raise Cancelled() from None
            raise ChatGPTError('ChatGPT 图片上传失败，请稍后重试。') from None
        self.json_request('/backend-api/files/' + file_id + '/uploaded', {}, job=job, stage='image_uploaded')
        record('chatgpt.web_image_uploaded', job['id'], bytes=len(data))
        return {'pointer': {'content_type': 'image_asset_pointer', 'asset_pointer': 'file-service://' + file_id,
                            'size_bytes': len(data), 'width': image.width, 'height': image.height},
                'metadata': {'id': file_id, 'name': 'clipboard.png', 'size': len(data), 'mime_type': 'image/png',
                             'width': image.width, 'height': image.height}}

    def checkpoint(self, job, stream):
        if stream.conversation_id:
            job['session_id'] = stream.conversation_id
        if stream.parent_id:
            job['assistant_id'] = stream.parent_id
        if stream.conversation_id and not job.get('saved_session'):
            update_registration(self.client_id, {'session_id': stream.conversation_id, 'parent_id': job['parent_id'], 'session_date': job['day']})
            job['saved_session'] = True

    def run(self, job):
        response = stream = None
        job['started_at'] = time.monotonic()
        try:
            record('chatgpt.web_request_started', job['id'], image=bool(job.get('image')))
            self.emit(job, 'progress', text='正在连接 ChatGPT')
            self.refresh_account()
            if not self.models:
                self.connect_account()
                job['model'] = self.model
            def prepare_session():
                self.daily_session(job)
                return self.upload(job) if job.get('image') else None
            def preparation_finished(pending):
                if pending.exception() is not None:
                    abort_io(job)
                    process = job.get('process')
                    if process and process.poll() is None:
                        try:
                            process.kill()
                        except OSError:
                            pass
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix='chatgpt-session') as preparation:
                pending = preparation.submit(prepare_session)
                pending.add_done_callback(preparation_finished)
                try:
                    if pending.done() and pending.exception() is not None:
                        raise pending.exception()
                    integrity = self.take_integrity(job)
                    attachment = pending.result()
                except Exception:
                    abort_io(job)
                    if pending.done() and pending.exception() is not None:
                        raise pending.exception()
                    raise
            if job['cancel'].is_set():
                raise Cancelled()
            record('chatgpt.web_prepared', job['id'], elapsed_ms=round((time.monotonic()-job['started_at'])*1000))
            payload = self.build_payload(job, attachment)
            self.emit(job, 'progress', text='正在发送')
            job['submitted'] = True
            for attempt in range(2):
                try:
                    response = self.request('/backend-api/f/conversation', payload,
                        {'Accept': 'text/event-stream', 'x-oai-mcp-form-version': '2', **integrity}, job=job, stage='completion')
                    break
                except CheckRejected:
                    if attempt or not job.get('_used_prefetched_check'):
                        raise
                    record('chatgpt.web_check_cache_rejected', job['id'])
                    self.emit(job, 'progress', text='正在重新连接')
                    integrity = self.take_integrity(job, force_new=True)
            self.checks.refill()
            if 'text/event-stream' not in response.headers.get('Content-Type', ''):
                raise ChatGPTError('ChatGPT 未返回流式回答，请稍后重试。')
            record('chatgpt.web_stream_started', job['id'], elapsed_ms=round((time.monotonic()-job['started_at'])*1000))
            self.emit(job, 'started')
            stream, previous, last_emit = WebStream(), None, 0
            for name, event in events(response):
                if job['cancel'].is_set():
                    raise Cancelled()
                stream.event(name, event)
                self.checkpoint(job, stream)
                if stream.error:
                    raise ChatGPTError(stream.error)
                content = stream.content()
                now = time.monotonic()
                if content['answer'] and not job.get('first_answer_at'):
                    job['first_answer_at'] = now
                    record('chatgpt.web_first_answer', job['id'], elapsed_ms=round((now-job['started_at'])*1000))
                if content != previous and any(content.values()) and (now - last_emit >= .035 or stream.done):
                    self.emit(job, 'delta', **content)
                    previous, last_emit = content, now
                if stream.done:
                    break
            if job['cancel'].is_set():
                raise Cancelled()
            record('chatgpt.web_stream_finished', job['id'], done=stream.done,
                   completed=stream.completed, messages=len(stream.order),
                   service_error=bool(stream.failure_notice), images=len(stream.content().get('images', [])))
            if not stream.completed:
                raise ChatGPTError('ChatGPT 回复中断，已收到的内容保留在弹窗中。')
            if any(image.get('kind')=='pending' for image in stream.content().get('images',[])):
                try:
                    history = self.json_request('/backend-api/conversation/'+stream.conversation_id,
                                                job=job,stage='rich_content')
                    stream.hydrate(history)
                except (ChatGPTError,ValueError,OSError):
                    record('chatgpt.web_rich_content_unavailable',job['id'])
            update_registration(self.client_id, {'session_id': stream.conversation_id,
                'parent_id': stream.parent_id, 'session_date': job['day']})
            notice = stream.failure_notice
            self.emit(job, 'complete', **stream.content(), failed=bool(notice), notice=notice)
            record('chatgpt.web_generation_failed' if notice else 'chatgpt.web_complete', job['id'])
        except Exception as error:
            if job['cancel'].is_set() or isinstance(error, Cancelled):
                self.finish_stop(job)
            else:
                frames = traceback.extract_tb(error.__traceback__)
                location = f'{frames[-1].name}:{frames[-1].lineno}' if frames else 'unknown'
                record('chatgpt.web_error', job['id'], error=type(error).__name__, location=location)
                self.emit(job, 'login' if isinstance(error, ChatGPTLoginRequired) else 'error',
                          text=str(error) if isinstance(error, ChatGPTError) else 'ChatGPT 连接中断，请重试；持续出现时请查看本机日志。')
        finally:
            if response:
                response.close()
            self.checks.refill()
            with self.lock:
                if self.active is job:
                    self.active = None

    def stop(self, request_id):
        with self.lock:
            job = self.active
        if job is None or job['id'] != request_id:
            self.events.put({'type': 'stopped', 'id': request_id, 'confirmed': True})
            return
        job['cancel'].set()
        abort_io(job)
        process = job.get('process')
        if process and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        response = job.get('response')
        if response:
            try:
                response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
            except (AttributeError, OSError):
                pass

    def close(self):
        super().close()
        self.checks.close()
        self.http.close()

    def finish_stop(self, job):
        confirmed = not job.get('submitted')
        if job.get('submitted') and job.get('session_id'):
            try:
                self.json_request('/backend-api/stop_conversation', {'conversation_id': job['session_id']}, stage='stop')
                confirmed = True
            except Exception:
                pass
        self.emit(job, 'stopped', confirmed=confirmed)
        record('chatgpt.web_stopped', job['id'], confirmed=confirmed)
