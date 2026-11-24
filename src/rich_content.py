"""Translate website display components to safe Markdown and image references."""
from hashlib import sha256
from html.parser import HTMLParser
import json
import re
from urllib.parse import urlsplit


IMAGE_SCHEME = 'desktop-ai-image://'
MAX_REPLY_IMAGES = 8
_code = re.compile(r'(?ms)(^[ \t]*```[^\n]*\n.*?^[ \t]*```[^\n]*$|^[ \t]*~~~[^\n]*\n.*?^[ \t]*~~~[^\n]*$|^[ \t]*(?:```|~~~).*\Z|`[^`\n]*`)')
_component = re.compile(r'<AsyncImage\b(?:"[^"]*"|\x27[^\x27]*\x27|[^"\x27>])*(?:/>|>\s*</AsyncImage\s*>)', re.I)
_private = re.compile('\ue200([a-z_]+)\ue202(.*?)\ue201', re.S)
_asset = re.compile(r'(?:sediment|file-service)://(file[-_][A-Za-z0-9_-]{8,128})\Z')


def asset_id(pointer):
    match = _asset.fullmatch(pointer) if isinstance(pointer, str) else None
    return match[1] if match else None


def image_key(value):
    return sha256(value.encode('utf-8')).hexdigest()[:24]


def https_url(value):
    if not isinstance(value, str) or len(value) > 32768:
        return None
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password else None
    except ValueError:
        return None


def has_asset(content):
    return any(isinstance(part, dict) and asset_id(part.get('asset_pointer'))
               for part in (content.get('parts') or []))


class Attributes(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values = {}

    def handle_starttag(self, tag, attrs):
        self.values = dict(attrs)

    handle_startendtag = handle_starttag


def message_content(message):
    metadata = message.get('metadata') or {}
    content = message.get('content') or {}
    model = metadata.get('model_dil_v2') or {}
    model = model if isinstance(model,dict) else {}
    app_data = model.get('appData') or {}
    app_data = app_data if isinstance(app_data,dict) else {}
    op = app_data.get('opGenui') or {}
    op = op if isinstance(op,dict) else {}
    components = op.get('componentResults') or {}
    candidates = []
    if isinstance(components, dict):
        for component in components.values():
            if not isinstance(component, dict):
                continue
            state = component.get('state') or {}
            for item in (state.get('images') or []) if isinstance(state, dict) else []:
                if not isinstance(item, dict):
                    continue
                url = https_url(item.get('content_url')) or https_url(item.get('thumbnail_url'))
                if url:
                    candidates.append({'kind':'remote', 'url':url,
                        'fallback_url':https_url(item.get('thumbnail_url')),
                        'source_url':https_url(item.get('url')), 'title':str(item.get('title') or '配图')[:300],
                        'query':str(item.get('image_search_query') or '')[:2000]})
                if len(candidates) >= MAX_REPLY_IMAGES:
                    break
    images, claimed = [], set()
    identifier = str(message.get('id') or '')

    def marker(item, seed):
        key = image_key(seed)
        if not any(image['key'] == key for image in images):
            if len(images) >= MAX_REPLY_IMAGES:
                return ''
            images.append({**item, 'key':key, 'message_id':identifier})
        return '\n\n![图片]('+IMAGE_SCHEME+key+')\n\n'

    def query_images(queries):
        output = []
        for query in queries[:MAX_REPLY_IMAGES]:
            if not isinstance(query, str):
                continue
            query = query[:2000]
            matches = [(index, item) for index, item in enumerate(candidates)
                       if item['query'].strip().casefold() == query.strip().casefold()]
            if not matches:
                matches = [(index, item) for index, item in enumerate(candidates) if not item['query'] and index not in claimed][:1]
            if matches:
                for number, (index, item) in enumerate(matches):
                    claimed.add(index)
                    output.append(marker(item, identifier+'|query|'+query+'|'+str(number)))
            else:
                output.append(marker({'kind':'pending', 'query':query, 'title':'配图'}, identifier+'|query|'+query+'|0'))
        return ''.join(output)

    def component(match):
        parser = Attributes()
        try:
            parser.feed(match[0])
        except Exception:
            return ''
        query = parser.values.get('query') or parser.values.get('alt') or ''
        direct = https_url(parser.values.get('src'))
        return marker({'kind':'remote','url':direct,'title':query or '配图'}, identifier+'|src|'+direct) if direct else query_images([query])

    def private(match):
        name, raw = match.groups()
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            return ''
        if name == 'image_group' and isinstance(value, dict):
            queries = value.get('query', value.get('queries', []))
            return query_images([queries] if isinstance(queries, str) else queries if isinstance(queries, list) else [])
        if name == 'entity' and isinstance(value, list) and len(value)>1 and isinstance(value[1], str):
            return value[1]
        return ''

    def clean(text):
        parts = _code.split(text)
        for index in range(0, len(parts), 2):
            value = _component.sub(component, parts[index])
            value = _private.sub(private, value)
            value = re.sub(r'<Cite\b(?:"[^"]*"|\x27[^\x27]*\x27|[^"\x27>])*/\s*>', '', value)
            value = re.sub(r'<(?:AsyncImage|Cite)\b[^>]*\Z', '', value)
            value = re.sub('\ue200[^\ue201]*\\Z', '', value)
            parts[index] = value
        return ''.join(parts)

    fallback = model.get('fallbackMarkdown')
    parts = content.get('parts') or []
    if message.get('author', {}).get('role') == 'tool':
        text = ''
    else:
        text = clean(fallback if isinstance(fallback, str) and fallback.strip() else ''.join(part for part in parts if isinstance(part, str)))
    for index, part in enumerate(parts):
        if not isinstance(part, dict) or not asset_id(part.get('asset_pointer')):
            continue
        pointer = part['asset_pointer']
        ready = message.get('status') == 'finished_successfully'
        item = {'kind':'generated' if ready else 'pending','asset_pointer':pointer,'title':'生成的图片' if ready else '图片生成中'}
        text += marker(item, identifier+'|asset|'+pointer)
    for index, item in enumerate(candidates):
        if index not in claimed:
            text += marker(item, identifier+'|extra|'+str(index))
    return text, images


def copyable_text(text):
    return re.sub(r'!\[([^\]]*)\]\(desktop-ai-image://[a-f0-9]+\)', '[图片]', text)
