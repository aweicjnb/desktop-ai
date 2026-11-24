"""Resolve user image assets and fetch media without leaking login headers."""
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, build_opener

from chatgpt_account import NoRedirect
from image_codec import ImageLoadError, MAX_BYTES
from http_transport import JobHTTPS
from rich_content import asset_id, https_url


def allowed_media_url(url):
    if not https_url(url):
        return False
    try:
        parsed = urlsplit(url)
        if parsed.port not in (None,443):
            return False
        host = parsed.hostname.lower()
        if host == 'chatgpt.com':
            return parsed.path == '/backend-api/estuary/content'
        return host == 'images.openai.com' or host.endswith(('.oaiusercontent.com','.oaiusercontent.cn','.blob.core.windows.net'))
    except ValueError:
        return False


def fetch_media(client, descriptor, job):
    pointer = descriptor.get('asset_pointer')
    if pointer:
        identifier = asset_id(pointer)
        if not identifier:
            raise ImageLoadError('无法识别这张图片，请在网页查看。')
        value = client.json_request('/backend-api/files/'+identifier+'/download', job=job, stage='output_image_link')
        url = value.get('download_url')
    else:
        url = descriptor.get('url')
    fallback = descriptor.get('fallback_url')
    for attempt in range(2 if fallback and fallback != url else 1):
        target = url if not attempt else fallback
        try:
            return fetch_url(client,target,job)
        except ImageLoadError:
            if attempt or not fallback or fallback == url:
                raise


def fetch_url(client, url, job):
    renewed = False
    for _ in range(4):
        if job['cancel'].is_set():
            raise ImageLoadError('图片加载已取消。')
        if not allowed_media_url(url):
            raise ImageLoadError('这张图片可在原网页查看。')
        parsed, account = urlsplit(url), client.account
        headers = {'User-Agent':account['user_agent'],'Accept':'image/png,image/jpeg,image/webp,image/avif,image/gif'}
        if parsed.hostname.lower() == 'chatgpt.com':
            headers.update(Authorization='Bearer '+account['access_token'],Cookie=account.get('cookie_header',''),
                           Origin='https://chatgpt.com',Referer='https://chatgpt.com/')
            headers['Oai-Device-Id'] = account['device_id']
        try:
            with build_opener(NoRedirect(),JobHTTPS(job,client.http)).open(Request(url,headers=headers),timeout=12) as response:
                raw = response.read(MAX_BYTES+1)
                if len(raw)>MAX_BYTES:
                    raise ImageLoadError('图片过大，请在网页查看。')
                return raw
        except HTTPError as error:
            status, location = error.code, error.headers.get('Location')
            error.close()
            if status in (301,302,303,307,308) and location:
                url = urljoin(url,location)
                continue
            if status==401 and parsed.hostname.lower()=='chatgpt.com' and not renewed:
                client.refresh_account(force=True);renewed=True
                continue
            raise ImageLoadError('图片暂未加载，请重试或在网页查看。') from None
        except (URLError,OSError,TimeoutError):
            raise ImageLoadError('图片加载超时，请重试。') from None
    raise ImageLoadError('图片地址暂不可用，请在网页查看。')
