"""Protected ChatGPT website sessions. No OAuth grant or extension is used."""
from base64 import urlsafe_b64decode
import hashlib
import json
import math
import time
import uuid

from chatgpt_account import (
    ChatGPTError, ChatGPTLoginRequired, account_lock, data_directory,
    read_protected, write_protected,
)


DEFAULT_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36'


def session_cookies(token, previous=''):
    pieces = [item.strip() for item in previous.split(';') if item.strip()
              and not item.strip().startswith(('__Secure-next-auth.session-token=', '__Secure-next-auth.session-token.'))]
    chunks = [token[index:index + 3933] for index in range(0, len(token), 3933)]
    name = '__Secure-next-auth.session-token'
    pieces.extend((f'{name}.{index}={chunk}' if len(chunks) > 1 else f'{name}={chunk}')
                  for index, chunk in enumerate(chunks))
    return '; '.join(pieces)


def normalize(value):
    if not isinstance(value, dict):
        raise ValueError('请导入 ChatGPT 网页会话 JSON。')
    token = value.get('access_token', value.get('accessToken'))
    if not isinstance(token, str) or len(token) > 32768:
        raise ValueError('ChatGPT 网页会话中没有可用的 accessToken。')
    try:
        claims = json.loads(urlsafe_b64decode(token.split('.')[1] + '==='))
        subject = claims['sub']
        audience = claims['aud']
        if 'https://api.openai.com/v1' not in ([audience] if isinstance(audience, str) else audience):
            raise ValueError()
        if not isinstance(subject, str) or not subject:
            raise ValueError()
        expiry = float(claims['exp'])
        if not math.isfinite(expiry):
            raise ValueError()
    except (ValueError, TypeError, KeyError, IndexError):
        raise ValueError('ChatGPT 网页会话格式不正确，请重新复制完整会话 JSON。') from None
    user = value.get('user') if isinstance(value.get('user'), dict) else {}
    profile = claims.get('https://api.openai.com/profile') if isinstance(claims.get('https://api.openai.com/profile'), dict) else {}
    email = value.get('email') or user.get('email') or profile.get('email') or 'Chrome 网页登录'
    account = {key: item for key, item in value.items() if key not in ('accessToken', 'sessionToken', 'user')}
    account.update(kind='web', access_token=token, subject=subject, expires_at=expiry,
                   client_id='web-' + hashlib.sha256(subject.encode()).hexdigest()[:24],
                   email=email, user_agent=value.get('user_agent') or DEFAULT_AGENT,
                   device_id=value.get('device_id') or str(uuid.uuid4()))
    if value.get('sessionToken'):
        account['cookie_header'] = session_cookies(value['sessionToken'], value.get('cookie_header', ''))
    account.setdefault('cookie_header', '')
    return account


def store_path():
    return data_directory() / 'chatgpt-web-accounts.bin'


def load_store():
    try:
        value = read_protected(store_path())
    except FileNotFoundError:
        value = {'registrations': {}}
        # Import the already verified local session from the connection probe.
        try:
            account = normalize(read_protected(data_directory() / 'chatgpt-web-account.bin'))
        except FileNotFoundError:
            return value
        value['registrations'][account['client_id']] = account
        value['active'] = account['client_id']
    if not isinstance(value, dict) or not isinstance(value.get('registrations'), dict):
        raise ValueError('ChatGPT 网页登录数据无法读取，请重新导入。')
    return value


def saved_accounts():
    return list(load_store()['registrations'].values())


def load_account(client_id=None):
    value = load_store()
    account = value['registrations'].get(client_id or value.get('active'))
    if not account:
        raise FileNotFoundError('请先导入 ChatGPT 网页登录。')
    return normalize(account)


def save_account(account, activate=False):
    account = normalize(account)
    with account_lock():
        value = load_store()
        previous = value['registrations'].get(account['client_id'], {})
        # Retain the conversation when refreshing/importing the same account.
        merged = {**previous, **account}
        for name in ('session_id', 'parent_id', 'session_date'):
            if name in previous:
                merged[name] = previous[name]
        value['registrations'][account['client_id']] = merged
        if activate:
            value['active'] = account['client_id']
        write_protected(store_path(), value)


def update_registration(client_id, fields=None, activate=False):
    with account_lock():
        value = load_store()
        value['registrations'][client_id].update(fields or {})
        if activate:
            value['active'] = client_id
        write_protected(store_path(), value)


def import_session(text):
    if not isinstance(text, str) or len(text) > 1024 * 1024:
        raise ValueError('请复制完整的 ChatGPT 网页会话 JSON 后再导入。')
    try:
        value = json.loads(text)
    except ValueError:
        raise ValueError('剪贴板中不是会话 JSON，请先在 Chrome 打开网页会话并复制。') from None
    return normalize(value)
