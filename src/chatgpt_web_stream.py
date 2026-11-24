"""ChatGPT full-message and v1 compressed SSE protocol."""
import copy
import json
import re

from chatgpt_account import ChatGPTError
from rich_content import has_asset, message_content


def events(response):
    name, data, size = '', [], 0
    for raw in response:
        if len(raw) > 4 * 1024 * 1024:
            raise ChatGPTError('ChatGPT 回复数据异常，已保留收到的内容。')
        line = raw.decode('utf-8').rstrip('\r\n')
        if line.startswith('event:'):
            name = line[6:].strip()
        elif line.startswith('data:'):
            data.append(line[5:].lstrip())
            size += len(line)
            if size > 4 * 1024 * 1024:
                raise ChatGPTError('ChatGPT 回复数据异常，已保留收到的内容。')
        elif not line and data:
            joined = '\n'.join(data)
            yield name, '[DONE]' if joined == '[DONE]' else json.loads(joined)
            name, data, size = '', [], 0
    if data:
        joined = '\n'.join(data)
        yield name, '[DONE]' if joined == '[DONE]' else json.loads(joined)


class WebStream:
    def __init__(self):
        self.root = {}
        self.path, self.operation = '', 'add'
        self.messages = {}
        self.order = []
        self.conversation_id = self.parent_id = None
        self.done = False
        self.searching = False
        self.error = None

    def patch(self, path, operation, value, prefix=''):
        if operation == 'patch':
            child_path, child_operation = '', 'add'
            for child in value:
                child_path, child_operation = child.get('p', child_path), child.get('o', child_operation)
                self.patch(child_path, child_operation, child.get('v'), prefix + path)
            return
        parts = [part.replace('~1', '/').replace('~0', '~') for part in (prefix + path).split('/') if part]
        if not parts:
            if operation == 'append' and isinstance(self.root,dict) and isinstance(value,dict):
                self.root.update(copy.deepcopy(value))
            else:
                self.root = copy.deepcopy(value)
            return
        node = self.root
        for part in parts[:-1]:
            node = node[int(part)] if isinstance(node, list) else node[part]
        key = (len(node) if parts[-1]=='-' else int(parts[-1])) if isinstance(node,list) else parts[-1]
        if operation == 'append':
            if isinstance(node[key], dict):
                node[key].update(copy.deepcopy(value))
            else:
                node[key] += copy.deepcopy(value)
        elif operation == 'add' and isinstance(node,list):
            node.insert(key,copy.deepcopy(value))
        elif operation in ('add', 'replace'):
            node[key] = copy.deepcopy(value)
        elif operation == 'remove':
            if isinstance(node, list):
                node.pop(key)
            else:
                node.pop(key, None)
        else:
            raise ChatGPTError('ChatGPT 回复格式已变化，请更新应用。')

    def event(self, name, event):
        if event == '[DONE]':
            self.done = True
            return
        if not isinstance(event, dict):
            return
        kind = event.get('type')
        if event.get('conversation_id'):
            self.conversation_id = event['conversation_id']
        if kind == 'error' or event.get('error'):
            self.error = 'ChatGPT 生成失败，已保留收到的内容，请稍后重试。'
            return
        if (kind and 'message' not in event and 'v' not in event) or name == 'delta_encoding':
            return
        if 'v' in event:
            self.path = event.get('p', self.path)
            self.operation = event.get('o', self.operation)
            self.patch(self.path, self.operation, event['v'])
        elif 'message' in event:
            self.root = event
        else:
            return
        if not isinstance(self.root, dict):
            return
        if self.root.get('error'):
            self.error = 'ChatGPT 生成失败，已保留收到的内容，请稍后重试。'
        self.conversation_id = self.root.get('conversation_id') or self.conversation_id
        message = self.root.get('message') or {}
        author = message.get('author') or {}
        if author.get('role') == 'tool' or message.get('recipient') in ('browser', 'web', 'web.run'):
            self.searching = True
        if (message.get('metadata') or {}).get('is_visually_hidden_from_conversation'):
            return
        content = message.get('content') or {}
        # Commentary addressed to the user also carries image-generation
        # results and refusals. It is separate from hidden analysis/tool calls.
        visible_text = (author.get('role') == 'assistant' and message.get('channel') in (None, 'final', 'commentary')
                        and content.get('content_type') in ('text', 'multimodal_text') and message.get('recipient') in (None, 'all'))
        # Tool names and image_gen_group_id are not stable: some successful
        # generations provide only image_gen_title and an obfuscated tool name.
        # The validated asset pointer in a user-visible tool result is enough.
        visible_image = (has_asset(content) and message.get('channel') in (None, 'final', 'commentary')
                         and message.get('recipient') in (None, 'all')
                         and author.get('role') in ('assistant', 'tool'))
        if not (visible_text or visible_image):
            return
        identifier = message.get('id')
        if identifier:
            self.parent_id = identifier
            if identifier not in self.messages:
                self.order.append(identifier)
            self.messages[identifier] = copy.deepcopy(message)

    @property
    def completed(self):
        last = self.messages.get(self.parent_id, {})
        return self.done and last.get('status') == 'finished_successfully' and bool(self.content()['answer'].strip())

    @property
    def failure_notice(self):
        last = self.messages.get(self.parent_id, {})
        if not (last.get('metadata') or {}).get('is_error'):
            return ''
        text = self.content()['answer'].lower()
        if 'similarity to third-party content' in text:
            return 'ChatGPT 未生成图片：服务端提示结果可能与第三方内容过于相似。请调整图片描述后再试。'
        return 'ChatGPT 未能完成生成，具体原因见上方回复。'

    def content(self):
        texts, sources, images = [], [], []
        for identifier in self.order:
            message = self.messages[identifier]
            text, media = message_content(message)
            texts.append(text)
            images.extend({**image,'conversation_id':self.conversation_id} for image in media)
            def collect(value):
                if isinstance(value, dict):
                    url = value.get('url')
                    if isinstance(url, str) and url.startswith(('https://', 'http://')):
                        pair = (str(value.get('title') or url), url)
                        if pair not in sources:
                            sources.append(pair)
                    for child in value.values():
                        collect(child)
                elif isinstance(value, list):
                    for child in value:
                        collect(child)
            metadata = message.get('metadata') or {}
            collect(metadata.get('content_references', []))
            collect(metadata.get('citations', []))
            safe_urls = {url for url in (metadata.get('safe_urls') or []) if isinstance(url, str)}
            for reference in (metadata.get('content_references') or []):
                if isinstance(reference, dict):
                    safe_urls.update(url for url in (reference.get('safe_urls') or []) if isinstance(url, str))
            reference_text = text + ''.join(part for part in (message.get('content',{}).get('parts') or []) if isinstance(part,str))
            reference_text += str((metadata.get('model_dil_v2') or {}).get('fallbackMarkdown') or '')
            cited = {(kind, int(index)) for kind, index in re.findall(r'turn\d+(search|fetch|news)(\d+)', reference_text)}
            for group in (metadata.get('search_result_groups') or []):
                if not isinstance(group, dict):
                    continue
                for entry in (group.get('entries') or []):
                    if not isinstance(entry, dict):
                        continue
                    reference = entry.get('ref_id') if isinstance(entry.get('ref_id'), dict) else {}
                    if (entry.get('url') in safe_urls or
                            (reference.get('ref_type'), reference.get('ref_index')) in cited):
                        collect(entry)
        answer = '\n\n'.join(texts)
        if self.done and sources:
            answer += '\n\n来源\n' + '\n'.join(
                '- [' + title.replace('[', '').replace(']', '') + '](' + url.replace(')', '%29').replace(' ', '%20') + ')'
                for title, url in sources)
        result = {'answer': answer, 'thought': '', 'search': ('done' if answer else 'searching') if self.searching else ''}
        if images:
            result['images'] = images
        return result

    def hydrate(self, history):
        mapping = history.get('mapping') or {}
        for identifier in self.order:
            message = (mapping.get(identifier) or {}).get('message')
            if isinstance(message, dict):
                self.messages[identifier] = copy.deepcopy(message)
