"""Offline regressions for website sessions and the actual popup transport."""
from base64 import urlsafe_b64encode
from datetime import date
from email.message import Message
import io
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import chatgpt_web_account as account
from chatgpt_web_client import ChatGPTWebClient, error_description
from chatgpt_web_stream import WebStream, events
from chatgpt_account import ChatGPTError, ChatGPTLoginRequired


SESSION = '239c1d1e-015b-4dfe-a264-c7bf5cbfa61f'
ASSISTANT = '48b0d0f4-73b1-471c-8a2a-2cfc89c10fe4'


def fixture(subject='test-user', expired=False):
    claims = {'aud': ['https://api.openai.com/v1'], 'sub': subject,
              'exp': time.time() + (-10 if expired else 3600),
              'https://api.openai.com/profile': {'email': 'test@example.invalid'}}
    encoded = urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    return account.normalize({'access_token': 'eyJhbGciOiJSUzI1NiJ9.' + encoded + '.test',
        'cookie_header': '__Secure-next-auth.session-token=test-session',
        'models': [{'slug': 'test-model', 'display_name': 'Test model'}], 'model': 'test-model'})


def job(**fields):
    return {'id': 'a' * 32, 'prompt': 'test', 'day': date.today().isoformat(),
            'model': 'test-model', 'search': False, 'thinking': False,
            'cancel': threading.Event(), **fields}


def web_events(text='answer'):
    message = {'id': ASSISTANT, 'author': {'role': 'assistant'}, 'channel': 'final', 'recipient': 'all',
               'content': {'content_type': 'text', 'parts': ['']}, 'metadata': {}, 'status': 'in_progress'}
    return [{'p': '', 'o': 'add', 'v': {'message': message, 'conversation_id': SESSION}},
            {'o': 'patch', 'v': [{'p': '/message/content/parts/0', 'o': 'append', 'v': text},
                               {'p': '/message/status', 'o': 'replace', 'v': 'finished_successfully'}]},
            {'type': 'message_stream_complete', 'conversation_id': SESSION}, '[DONE]']


class Response(io.BytesIO):
    def __init__(self, value, stream=False, cookie=None):
        raw = b''.join(('data: ' + (item if item == '[DONE]' else json.dumps(item)) + '\n\n').encode()
                       for item in value) if stream else json.dumps(value).encode()
        super().__init__(raw)
        self.headers = Message()
        self.headers['Content-Type'] = 'text/event-stream' if stream else 'application/json'
        if cookie:
            self.headers['Set-Cookie'] = cookie


class Isolated(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix='desktop-ai-web-test-')
        self.environ = patch.dict(os.environ, {'LOCALAPPDATA': self.temporary.name})
        self.environ.start()

    def tearDown(self):
        self.environ.stop()
        self.temporary.cleanup()

    def make_client(self, value=None):
        value = value or fixture()
        account.save_account(value, activate=True)
        return ChatGPTWebClient(value)


class AccountTests(Isolated):
    def test_invalid_clipboard_is_rejected_without_replacing_account(self):
        saved = fixture()
        account.save_account(saved, activate=True)
        for value in ('hello', '{}', json.dumps({'accessToken': 'invalid'})):
            with self.assertRaises(ValueError):
                account.import_session(value)
        self.assertEqual(account.load_account()['access_token'], saved['access_token'])

    def test_session_json_import_and_chunked_cookie(self):
        saved = fixture()
        imported = account.import_session(json.dumps({'accessToken': saved['access_token'],
            'sessionToken': 'x' * 5000, 'user': {'email': 'other@example.invalid'}}))
        self.assertIn('__Secure-next-auth.session-token.0=', imported['cookie_header'])
        self.assertIn('__Secure-next-auth.session-token.1=', imported['cookie_header'])
        self.assertEqual(imported['email'], 'other@example.invalid')

    def test_accounts_are_protected_and_have_separate_conversations(self):
        first, second = fixture(), fixture('second-user')
        first['session_id'] = SESSION
        account.save_account(first, activate=True)
        account.save_account(second)
        self.assertEqual(account.load_account()['client_id'], first['client_id'])
        self.assertNotIn('session_id', account.load_account(second['client_id']))
        self.assertNotIn(first['access_token'].encode(), account.store_path().read_bytes())

    def test_session_refresh_preserves_context_and_rotates_cookie(self):
        original = fixture(expired=True)
        original.update(session_id=SESSION, session_date=date.today().isoformat())
        transport = self.make_client(original)
        renewed = fixture()
        response = Response({'accessToken': renewed['access_token'], 'sessionToken': 'renewed-session',
                             'user': {'email': 'test@example.invalid'}}, cookie='cf_clearance=renewed-clearance; Secure')
        with patch.object(transport, 'request', return_value=response):
            transport.refresh_account()
        saved = account.load_account()
        self.assertEqual(saved['session_id'], SESSION)
        self.assertIn('renewed-session', saved['cookie_header'])
        self.assertIn('renewed-clearance', saved['cookie_header'])

    def test_reconnect_does_not_restore_a_stale_conversation(self):
        saved = fixture()
        saved['session_id'] = SESSION
        account.save_account(saved, activate=True)
        account.update_registration(saved['client_id'], {'session_id': None})
        account.save_account(saved)
        self.assertIsNone(account.load_account().get('session_id'))

    def test_failed_refresh_preserves_previous_credentials(self):
        original = fixture(expired=True)
        transport = self.make_client(original)
        with patch.object(transport, 'request', return_value=Response({'error': 'RefreshAccessTokenError'})):
            with self.assertRaises(ChatGPTLoginRequired):
                transport.refresh_account()
        self.assertEqual(account.load_account()['access_token'], original['access_token'])

    def test_refresh_does_not_switch_account_identity(self):
        transport = self.make_client(fixture(expired=True))
        with patch.object(transport, 'request', return_value=Response({'accessToken': fixture('other')['access_token']})):
            with self.assertRaises(ChatGPTLoginRequired):
                transport.refresh_account()


class StreamTests(unittest.TestCase):
    def test_compressed_reply_requires_done_and_completed_assistant(self):
        stream = WebStream()
        for event in web_events()[:-1]:
            stream.event('', event)
        self.assertEqual(stream.content()['answer'], 'answer')
        self.assertFalse(stream.completed)
        stream.event('', '[DONE]')
        self.assertTrue(stream.completed)
        self.assertEqual(stream.parent_id, ASSISTANT)

    def test_hidden_reasoning_and_tool_messages_are_not_shown(self):
        stream = WebStream()
        for role, channel, text in [('user', None, 'question'), ('assistant', 'analysis', 'private reasoning'),
                                    ('tool', None, 'tool result'), ('assistant', 'final', 'visible')]:
            stream.event('', {'message': {'id': role + str(channel), 'author': {'role': role}, 'channel': channel,
                'content': {'content_type': 'text', 'parts': [text]}, 'status': 'finished_successfully'}, 'conversation_id': SESSION})
        stream.event('', '[DONE]')
        self.assertEqual(stream.content()['answer'], 'visible')
        self.assertEqual(stream.content()['thought'], '')

    def test_nullable_metadata_and_recipient_are_supported(self):
        stream = WebStream()
        stream.event('', {'message': {'id': ASSISTANT, 'author': {'role': 'assistant'},
            'recipient': None, 'channel': 'final', 'metadata': None,
            'content': {'content_type': 'text', 'parts': ['answer']}, 'status': 'finished_successfully'}})
        stream.event('', '[DONE]')
        self.assertTrue(stream.completed)
        self.assertEqual(stream.content()['answer'], 'answer')

    def test_sse_handles_comments_multiline_and_terminal_marker(self):
        response = Response(web_events(), stream=True)
        stream = WebStream()
        for name, event in events(response):
            stream.event(name, event)
        self.assertTrue(stream.completed)

    def test_server_error_does_not_become_a_completed_reply(self):
        stream = WebStream()
        stream.event('', {'type': 'error', 'error': 'failed'})
        stream.event('', '[DONE]')
        self.assertTrue(stream.error)
        self.assertFalse(stream.completed)

    def test_search_sources_are_safe_markdown_links(self):
        stream = WebStream()
        for event in web_events('result\ue200cite\ue202turn0search0\ue201'):
            stream.event('', event)
        stream.messages[ASSISTANT]['metadata']['content_references'] = [{'url': 'https://example.invalid/source', 'title': 'Source'}]
        text = stream.content()['answer']
        self.assertIn('[Source](https://example.invalid/source)', text)
        self.assertNotIn('\ue200', text)

    def test_web_cite_component_uses_cited_search_results(self):
        stream = WebStream()
        for event in web_events('Fact <Cite refs={["turn404173search0"]}/>'):
            stream.event('', event)
        stream.messages[ASSISTANT]['metadata']['search_result_groups'] = [{'entries': [
            {'ref_id': {'ref_type': 'search', 'ref_index': 0}, 'url': 'https://example.invalid/cited', 'title': 'Cited'},
            {'ref_id': {'ref_type': 'search', 'ref_index': 1}, 'url': 'https://example.invalid/unused', 'title': 'Unused'}]}]
        text = stream.content()['answer']
        self.assertNotIn('<Cite', text)
        self.assertIn('[Cited](https://example.invalid/cited)', text)
        self.assertNotIn('Unused', text)


class ClientTests(Isolated):
    def test_completed_image_with_hidden_empty_stub_allows_followup(self):
        value=fixture()
        value.update(session_id=SESSION,session_date=date.today().isoformat())
        transport=self.make_client(value)
        stub={'author':{'role':'assistant'},'status':'in_progress','metadata':{'is_visually_hidden_from_conversation':True},
              'content':{'content_type':'text','parts':['']}}
        image={'author':{'role':'tool'},'status':'finished_successfully','channel':'commentary','recipient':'all',
               'content':{'content_type':'multimodal_text','parts':[{'asset_pointer':'sediment://file_1234567890abcdef1234567890abcdef'}]}}
        history={'current_node':'hidden-stub','mapping':{'hidden-stub':{'message':stub,'parent':ASSISTANT},ASSISTANT:{'message':image}}}
        current=job()
        with patch.object(transport,'json_request',return_value=history):
            transport.daily_session(current)
        self.assertEqual(current['parent_id'],ASSISTANT)
        self.assertEqual(current['session_id'],SESSION)

    def test_hidden_stub_does_not_allow_followup_when_image_is_still_pending(self):
        value=fixture()
        value.update(session_id=SESSION,session_date=date.today().isoformat())
        transport=self.make_client(value)
        stub={'author':{'role':'assistant'},'status':'in_progress','metadata':{'is_visually_hidden_from_conversation':True},
              'content':{'content_type':'text','parts':['']}}
        pending={'author':{'role':'tool'},'status':'in_progress','channel':'commentary','recipient':'all',
                 'content':{'content_type':'multimodal_text','parts':[{'asset_pointer':'sediment://file_1234567890abcdef1234567890abcdef'}]}}
        history={'current_node':'hidden-stub','mapping':{'hidden-stub':{'message':stub,'parent':ASSISTANT},ASSISTANT:{'message':pending}}}
        with patch.object(transport,'json_request',return_value=history),self.assertRaisesRegex(ChatGPTError,'仍在生成'):
            transport.daily_session(job())

    def test_history_sync_overlaps_preparation(self):
        transport = self.make_client()
        history_started, preparation_started = threading.Event(), threading.Event()
        def history(current):
            history_started.set()
            if not preparation_started.wait(1):
                raise AssertionError('session sync is blocking preparation')
            current.update(session_id=None, parent_id=ASSISTANT)
        def prepare(current):
            self.assertTrue(history_started.wait(1))
            preparation_started.set()
            return {}
        with patch.object(transport, 'daily_session', side_effect=history), patch.object(
                transport, 'integrity', side_effect=prepare), patch.object(
                transport, 'request', return_value=Response(web_events(), stream=True)):
            transport.run(job())
        self.assertEqual(list(transport.events.queue)[-1]['type'], 'complete')

    def test_failed_session_preparation_does_not_send(self):
        transport = self.make_client()
        with patch.object(transport, 'daily_session', side_effect=ChatGPTError('session unavailable')), patch.object(
                transport, 'integrity', return_value={}), patch.object(transport, 'request') as request:
            transport.run(job())
        request.assert_not_called()
        self.assertEqual(list(transport.events.queue)[-1]['type'], 'error')

    def test_cancel_during_preparation_does_not_send(self):
        transport = self.make_client()
        current = job()
        def prepare(_):
            current['cancel'].set()
            return {}
        with patch.object(transport, 'integrity', side_effect=prepare), patch.object(transport, 'request') as request:
            transport.run(current)
        request.assert_not_called()
        self.assertEqual(list(transport.events.queue)[-1]['type'], 'stopped')

    def test_history_failure_interrupts_other_preparation_and_preserves_error(self):
        from unittest.mock import Mock
        transport = self.make_client()
        current = job()
        started, stopped = threading.Event(), threading.Event()
        def history(_):
            if not started.wait(1):
                raise AssertionError('other preparation did not start')
            raise ChatGPTError('session unavailable')
        def prepare(_):
            process = Mock()
            process.poll.return_value = None
            process.kill.side_effect = stopped.set
            current['process'] = process
            started.set()
            self.assertTrue(stopped.wait(1), 'failed preparation should interrupt the helper')
            raise ChatGPTError('interrupted')
        with patch.object(transport, 'daily_session', side_effect=history), patch.object(
                transport, 'integrity', side_effect=prepare), patch.object(transport, 'request') as request:
            transport.run(current)
        request.assert_not_called()
        last = list(transport.events.queue)[-1]
        self.assertEqual(last['type'], 'error')
        self.assertEqual(last['text'], 'session unavailable')

    def test_daily_session_reuses_today_and_resets_next_day(self):
        saved = fixture()
        saved.update(session_id=SESSION, parent_id=ASSISTANT, session_date='2026-11-24')
        transport = self.make_client(saved)
        with patch.object(transport, 'json_request', return_value={'current_node': ASSISTANT}) as request:
            current = job(day='2026-11-24')
            transport.daily_session(current)
            self.assertEqual(current['session_id'], SESSION)
            self.assertEqual(current['parent_id'], ASSISTANT)
            tomorrow = job(day='2026-11-25')
            transport.daily_session(tomorrow)
            self.assertIsNone(tomorrow['session_id'])
            self.assertEqual(request.call_count, 1)

    def test_payload_retains_conversation_and_search_setting(self):
        transport = self.make_client()
        payload = transport.build_payload(job(session_id=SESSION, parent_id=ASSISTANT, search=True))
        self.assertEqual(payload['conversation_id'], SESSION)
        self.assertEqual(payload['parent_message_id'], ASSISTANT)
        self.assertTrue(payload['force_use_search'])
        self.assertEqual(payload['system_hints'], ['search'])

    def test_thinking_uses_an_available_reasoning_model(self):
        transport = self.make_client()
        transport.models.append({'slug': 'thinking-model', 'display_name': 'Thinking'})
        transport.account['categories'] = [{'category': 'reasoning', 'model_lane': 'thinking_mini', 'default_model': 'thinking-model'}]
        payload = transport.build_payload(job(parent_id=ASSISTANT, thinking=True))
        self.assertEqual(payload['model'], 'thinking-model')

    def test_in_progress_previous_reply_is_reported(self):
        saved = fixture()
        saved.update(session_id=SESSION, session_date=date.today().isoformat())
        transport = self.make_client(saved)
        response = {'current_node': ASSISTANT, 'mapping': {ASSISTANT: {'message': {
            'author': {'role': 'assistant'}, 'status': 'in_progress'}}}}
        with patch.object(transport, 'json_request', return_value=response):
            with self.assertRaisesRegex(ChatGPTError, '仍在生成'):
                transport.daily_session(job())

    def test_completed_turn_saves_position(self):
        transport = self.make_client()
        with patch.object(transport, 'integrity', return_value={}), patch.object(transport, 'request', return_value=Response(web_events(), stream=True)):
            transport.run(job())
        self.assertEqual(list(transport.events.queue)[-1]['type'], 'complete')
        saved = account.load_account()
        self.assertEqual(saved['session_id'], SESSION)
        self.assertEqual(saved['parent_id'], ASSISTANT)

    def test_truncated_stream_keeps_visible_partial_answer(self):
        transport = self.make_client()
        with patch.object(transport, 'integrity', return_value={}), patch.object(transport, 'request', return_value=Response(web_events()[:-1], stream=True)):
            transport.run(job())
        queued = list(transport.events.queue)
        self.assertTrue(any(event['type'] == 'delta' and event['answer'] == 'answer' for event in queued))
        self.assertEqual(queued[-1]['type'], 'error')

    def test_cancel_before_send_does_not_stop_an_existing_conversation(self):
        transport = self.make_client()
        current = job(session_id=SESSION, submitted=False)
        with patch.object(transport, 'json_request') as request:
            transport.finish_stop(current)
        request.assert_not_called()
        self.assertTrue(list(transport.events.queue)[-1]['confirmed'])

    def test_explicit_reset_and_http_error_classification(self):
        transport = self.make_client()
        transport.reset_session()
        self.assertIsNone(account.load_account().get('session_id'))
        self.assertIsInstance(error_description(401, b'{}', 'completion'), ChatGPTLoginRequired)
        self.assertIn('额度', str(error_description(429, b'{}', 'completion')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
