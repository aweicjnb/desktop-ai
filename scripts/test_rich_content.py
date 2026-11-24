"""Website rich components, image transport, decoding and the actual Tk view."""
import gc
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from chatgpt_web_stream import WebStream
from chatgpt_media import allowed_media_url, fetch_url
from image_codec import ImageLoadError, MAX_BYTES, RUNTIME, decode_image, pillow
from reply_images import ReplyImages
from rich_content import IMAGE_SCHEME, copyable_text, message_content
from test_chatgpt_web import Isolated, Response, fixture, job, SESSION
from chatgpt_web_client import ChatGPTWebClient
import chatgpt_web_account as account


URL='https://images.openai.com/test.png'
POINTER='sediment://file_1234567890abcdef1234567890abcdef'


def message(text='',role='assistant',content_type='text',parts=None,metadata=None,status='finished_successfully',channel='final'):
    return {'id':'message-id','author':{'role':role},'channel':channel,'recipient':'all','status':status,
            'content':{'content_type':content_type,'parts':parts if parts is not None else [text]},'metadata':metadata or {}}


def component(query='sweet potato'):
    return {'model_dil_v2':{'code':'throw new Error("must never execute")',
        'appData':{'opGenui':{'componentResults':{'component-id':{'status':'resolved','componentName':'AsyncImage',
        'state':{'images':[{'content_url':URL,'thumbnail_url':URL,'url':'https://example.com/source',
                           'image_search_query':query,'title':'Photo'}]}}}}}}}


class ParserTests(unittest.TestCase):
    def test_image_generation_refusal_in_commentary_preserves_service_message(self):
        stream=WebStream()
        text='The image may violate our guardrails concerning similarity to third-party content.'
        stream.event('',{'message':message(text,channel='commentary',metadata={'is_error':True})})
        stream.event('','[DONE]')
        self.assertTrue(stream.completed)
        self.assertEqual(stream.content()['answer'],text)
        self.assertIn('第三方内容过于相似',stream.failure_notice)
        self.assertFalse(stream.error)

    def test_user_visible_commentary_does_not_expose_analysis_or_tool_calls(self):
        stream=WebStream()
        for index,(channel,recipient,hidden) in enumerate((('analysis','all',False),
                ('commentary','image_gen',False),('commentary','all',True))):
            value=message('internal',channel=channel,metadata={'is_error':True,'is_visually_hidden_from_conversation':hidden})
            value.update(id=str(index),recipient=recipient)
            stream.event('',{'message':value})
        self.assertEqual(stream.content()['answer'],'')
        self.assertEqual(stream.failure_notice,'')

    def test_non_image_service_failure_keeps_original_reason(self):
        stream=WebStream()
        stream.event('',{'message':message('Image generation is unavailable.',channel='commentary',metadata={'is_error':True})})
        stream.event('','[DONE]')
        self.assertTrue(stream.completed)
        self.assertEqual(stream.failure_notice,'ChatGPT 未能完成生成，具体原因见上方回复。')

    def test_nullable_streaming_parts_and_component_images_do_not_crash(self):
        stream=WebStream();value=message(status='in_progress');value['content']['parts']=None
        stream.event('',{'message':value});self.assertEqual(stream.content()['answer'],'')
        metadata=component();metadata['model_dil_v2']['appData']['opGenui']['componentResults']['component-id']['state']['images']=None
        _,images=message_content(message('<AsyncImage query="sweet potato"/>',metadata=metadata))
        self.assertEqual(images[0]['kind'],'pending')
    def test_async_image_uses_resolved_metadata_and_preserves_table(self):
        source='<AsyncImage query="sweet potato" aspectRatio="16:9" width="100%"/>\n\n| A | B |\n|---|---|\n| 1 | 2 |'
        text,images=message_content(message(source,metadata=component()))
        self.assertNotIn('AsyncImage',text);self.assertIn('| A | B |',text)
        self.assertEqual(len(images),1);self.assertEqual(images[0]['url'],URL)
        self.assertIn(IMAGE_SCHEME+images[0]['key'],text)

    def test_fallback_image_group_is_resolved_without_running_model_code(self):
        metadata=component()
        metadata['model_dil_v2']['fallbackMarkdown']='before\n\ue200image_group\ue202'+json.dumps({'query':['sweet potato']})+'\ue201\nafter'
        text,images=message_content(message('other raw text',metadata=metadata))
        self.assertIn('before',text);self.assertIn('after',text);self.assertNotIn('\ue200',text)
        self.assertEqual(len(images),1)

    def test_code_examples_keep_literal_component_tags(self):
        source='`<AsyncImage query="example"/>`\n\n```jsx\n<AsyncImage query="example"/>\n```'
        text,images=message_content(message(source))
        self.assertEqual(text,source);self.assertFalse(images)

    def test_partial_streaming_tag_is_not_shown_as_text(self):
        text,images=message_content(message('hello <AsyncImage query="sweet'))
        self.assertEqual(text,'hello ');self.assertFalse(images)

    def test_pending_and_resolved_image_share_the_same_key(self):
        source='<AsyncImage query="sweet potato"/>'
        _,pending=message_content(message(source))
        _,resolved=message_content(message(source,metadata=component()))
        self.assertEqual(pending[0]['key'],resolved[0]['key'])
        self.assertEqual(pending[0]['kind'],'pending');self.assertEqual(resolved[0]['kind'],'remote')

    def test_quoted_angle_bracket_in_query_is_not_a_tag_boundary(self):
        text,images=message_content(message('<AsyncImage query="sweet > potato"/>'))
        self.assertNotIn('AsyncImage',text);self.assertEqual(images[0]['query'],'sweet > potato')

    def test_image_only_tool_reply_is_a_completed_answer(self):
        stream=WebStream()
        stream.event('',{'conversation_id':'conversation','message':message(role='tool',channel='commentary',
            content_type='multimodal_text',parts=['internal revised prompt',{'content_type':'image_asset_pointer','asset_pointer':POINTER}],
            metadata={'image_gen_group_id':'group'})})
        stream.event('','[DONE]')
        self.assertTrue(stream.completed)
        self.assertEqual(stream.content()['images'][0]['kind'],'generated')
        self.assertNotIn('internal revised prompt',stream.content()['answer'])

    def test_generated_asset_without_group_id_and_hidden_final_tail_completes(self):
        stream=WebStream()
        value=message(role='tool',channel='commentary',content_type='multimodal_text',
                      parts=[{'content_type':'image_asset_pointer','asset_pointer':POINTER}],
                      metadata={'image_gen_title':'Original illustration'})
        value['author']['name']='obfuscated.tool'
        stream.event('',{'message':value})
        stream.event('',{'message':message('',status='in_progress',metadata={'is_visually_hidden_from_conversation':True})})
        stream.event('','[DONE]')
        self.assertTrue(stream.completed)
        self.assertEqual(stream.content()['images'][0]['kind'],'generated')
        self.assertFalse(stream.failure_notice)

    def test_assets_addressed_to_other_tools_are_not_shown(self):
        stream=WebStream()
        value=message(role='tool',channel='commentary',content_type='multimodal_text',
                      parts=[{'content_type':'image_asset_pointer','asset_pointer':POINTER}])
        value['recipient']='another_tool'
        stream.event('',{'message':value})
        self.assertFalse(stream.content().get('images'))

    def test_analysis_images_are_not_exposed(self):
        stream=WebStream()
        stream.event('',{'message':message(role='tool',channel='analysis',content_type='multimodal_text',
             parts=[{'asset_pointer':POINTER}],metadata={'image_gen_group_id':'group'})})
        self.assertFalse(stream.content().get('images'))

    def test_compressed_array_add_and_append_for_image_parts(self):
        stream=WebStream()
        stream.event('',{'message':message(content_type='multimodal_text',status='in_progress')})
        stream.event('',{'p':'/message/content/parts/-','o':'add','v':{'content_type':'image_asset_pointer','asset_pointer':POINTER}})
        stream.event('',{'p':'/message/status','o':'replace','v':'finished_successfully'})
        stream.event('','[DONE]')
        self.assertTrue(stream.completed);self.assertEqual(len(stream.content()['images']),1)

    def test_hydration_updates_only_messages_seen_in_the_current_reply(self):
        stream=WebStream();stream.event('',{'message':message('<AsyncImage query="sweet potato"/>')})
        stream.hydrate({'mapping':{'message-id':{'message':message('<AsyncImage query="sweet potato"/>',metadata=component())},
                                 'old-message':{'message':message('old private answer')}}})
        self.assertEqual(stream.content()['images'][0]['kind'],'remote')
        self.assertNotIn('old private answer',stream.content()['answer'])

    def test_copy_does_not_expose_internal_image_addresses(self):
        text,_=message_content(message('<AsyncImage query="sweet potato"/>'))
        copied=copyable_text(text)
        self.assertIn('[图片]',copied);self.assertNotIn(IMAGE_SCHEME,copied)


class MediaSecurityTests(unittest.TestCase):
    def test_only_expected_https_image_hosts_and_paths_are_accepted(self):
        for url in (URL,'https://files.oaiusercontent.com/image.png','https://a.blob.core.windows.net/file',
                    'https://chatgpt.com/backend-api/estuary/content?id=file_123'):
            self.assertTrue(allowed_media_url(url),url)
        for url in ('file:///C:/private.txt','http://images.openai.com/test.png','https://127.0.0.1/test',
                    'https://images.openai.com.evil.invalid/test','https://user:password@images.openai.com/test',
                    'https://images.openai.com:8443/test','https://chatgpt.com/backend-api/accounts'):
            self.assertFalse(allowed_media_url(url),url)

    def test_redirect_to_cdn_drops_authorization_and_cookie(self):
        client=SimpleNamespace(account={'access_token':'private-token','cookie_header':'private-cookie',
                                       'user_agent':'test','device_id':'device'},http=None)
        requests=[]
        def open_request(request,timeout):
            requests.append(request)
            if len(requests)==1:
                raise HTTPError(request.full_url,302,'redirect',{'Location':URL},io.BytesIO())
            return io.BytesIO(b'image-bytes')
        with patch('chatgpt_media.build_opener') as build:
            build.return_value.open.side_effect=open_request
            result=fetch_url(client,'https://chatgpt.com/backend-api/estuary/content?id=file_123',{'cancel':threading.Event()})
        self.assertEqual(result,b'image-bytes')
        self.assertTrue(requests[0].get_header('Authorization'))
        self.assertFalse(requests[1].get_header('Authorization'));self.assertFalse(requests[1].get_header('Cookie'))

    def test_redirect_to_untrusted_host_is_stopped_before_fetch(self):
        client=SimpleNamespace(account={'user_agent':'test'},http=None)
        with patch('chatgpt_media.build_opener') as build:
            build.return_value.open.side_effect=HTTPError(URL,302,'redirect',{'Location':'https://evil.invalid/image'},io.BytesIO())
            with self.assertRaises(ImageLoadError):fetch_url(client,URL,{'cancel':threading.Event()})
            self.assertEqual(build.return_value.open.call_count,1)


class ClientImageTests(Isolated):
    def setUp(self):
        super().setUp()
        saved=fixture();account.save_account(saved,activate=True)
        self.client=ChatGPTWebClient(saved)

    def tearDown(self):
        self.client.close();super().tearDown()

    def test_pure_image_completion_reaches_the_popup_event(self):
        image_message=message(role='tool',channel='commentary',content_type='multimodal_text',
            parts=[{'content_type':'image_asset_pointer','asset_pointer':POINTER}],metadata={'image_gen_group_id':'group'})
        values=[{'message':image_message,'conversation_id':SESSION},'[DONE]']
        with patch.object(self.client,'integrity',return_value={}), patch.object(self.client,'request',return_value=Response(values,stream=True)):
            self.client.run(job())
        event=list(self.client.events.queue)[-1]
        self.assertEqual(event['type'],'complete');self.assertEqual(event['images'][0]['kind'],'generated')

    def test_generated_asset_without_optional_metadata_reaches_popup(self):
        value=message(role='tool',channel='commentary',content_type='multimodal_text',
                      parts=[{'content_type':'image_asset_pointer','asset_pointer':POINTER}],
                      metadata={'image_gen_title':'Original illustration'})
        value['author']['name']='obfuscated.tool'
        hidden=message('',status='in_progress',metadata={'is_visually_hidden_from_conversation':True})
        values=[{'message':value,'conversation_id':SESSION},{'message':hidden,'conversation_id':SESSION},'[DONE]']
        with patch.object(self.client,'integrity',return_value={}), patch.object(self.client,'request',return_value=Response(values,stream=True)):
            self.client.run(job())
        event=list(self.client.events.queue)[-1]
        self.assertEqual(event['type'],'complete');self.assertFalse(event['failed'])
        self.assertEqual(event['images'][0]['kind'],'generated')

    def test_image_refusal_reaches_popup_as_service_failure_not_interrupted_stream(self):
        refusal=message('The image may violate our guardrails concerning similarity to third-party content.',
                        channel='commentary',metadata={'is_error':True})
        values=[{'message':refusal,'conversation_id':SESSION},'[DONE]']
        with patch.object(self.client,'integrity',return_value={}), patch.object(self.client,'request',return_value=Response(values,stream=True)):
            self.client.run(job())
        queued=list(self.client.events.queue);event=queued[-1]
        self.assertEqual(event['type'],'complete');self.assertTrue(event['failed'])
        self.assertIn('第三方内容过于相似',event['notice'])
        self.assertIn('third-party content',event['answer'])
        self.assertFalse(any(value['type']=='error' for value in queued))

    def test_final_pending_component_fetches_resolved_metadata(self):
        pending=message('<AsyncImage query="sweet potato"/>')
        resolved=message('<AsyncImage query="sweet potato"/>',metadata=component())
        values=[{'message':pending,'conversation_id':SESSION},'[DONE]']
        with patch.object(self.client,'integrity',return_value={}), patch.object(self.client,'request',return_value=Response(values,stream=True)), patch.object(
                self.client,'json_request',return_value={'mapping':{'message-id':{'message':resolved}}}):
            self.client.run(job())
        event=list(self.client.events.queue)[-1]
        self.assertEqual(event['type'],'complete');self.assertEqual(event['images'][0]['kind'],'remote')

    def test_pending_image_worker_resolves_its_own_message(self):
        pending=message('<AsyncImage query="sweet potato"/>')
        _,images=message_content(pending);descriptor={**images[0],'conversation_id':SESSION}
        resolved=message('<AsyncImage query="sweet potato"/>',metadata=component())
        with patch.object(self.client,'json_request',return_value={'mapping':{'message-id':{'message':resolved}}}), patch(
                'chatgpt_media.fetch_media',return_value=b'image') as fetch:
            self.assertEqual(self.client.load_reply_image(descriptor,{'cancel':threading.Event()}),b'image')
        self.assertEqual(fetch.call_args.args[1]['url'],URL)


@unittest.skipUnless((RUNTIME/'version.txt').is_file(),'Prepare the image runtime before image tests')
class ImageTests(unittest.TestCase):
    def setUp(self):
        Image,_,_=pillow()
        image=Image.new('RGB',(180,120),(90,100,200))
        output=io.BytesIO();image.save(output,format='JPEG');self.raw=output.getvalue()

    def test_jpeg_webp_and_png_decode_for_the_tk_renderer(self):
        Image,_,_=pillow()
        for format in ('JPEG','WEBP','PNG'):
            output=io.BytesIO();Image.new('RGB',(180,120)).save(output,format=format)
            loaded=decode_image(output.getvalue())
            self.assertEqual((loaded.width,loaded.height),(180,120))

    def test_non_image_and_oversized_input_are_rejected(self):
        for value in (b'<html>not an image</html>',b'x'*(MAX_BYTES+1)):
            with self.assertRaises(ImageLoadError):decode_image(value)

    def test_previous_reply_download_cannot_populate_a_new_reply(self):
        loader=ReplyImages();started=threading.Event();release=threading.Event()
        def fetch(descriptor,current):
            started.set();release.wait(1);return self.raw
        try:
            loader.submit('old',0,{},fetch);self.assertTrue(started.wait(1))
            loader.clear();release.set()
            loader.submit('new',0,{},lambda descriptor,current:self.raw)
            generation,key,revision,loaded,error=loader.results.get(timeout=2)
            self.assertEqual(key,'new');self.assertEqual(generation,loader.generation);self.assertIsNone(error)
        finally:
            loader.close()
            for thread in loader.threads:thread.join(2)


@unittest.skipUnless((RUNTIME/'version.txt').is_file(),'Prepare the image runtime before GUI tests')
class ImageViewTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from popup import DARK
        from answer_view import AnswerView
        Image,_,_=pillow()
        output=io.BytesIO();Image.new('RGB',(180,120),(90,100,200)).save(output,format='JPEG')
        self.raw=output.getvalue();self.root=tk.Tk();self.root.withdraw()
        self.root.geometry('640x500+15000+15000')
        self.view=AnswerView(self.root,DARK,1);self.view.pack(fill='both',expand=True)
        self.root.deiconify();self.root.update()

    def tearDown(self):
        loader=self.view.image_loader
        self.root.destroy()
        for thread in loader.threads:thread.join(2)
        self.root=self.view=None;gc.collect()

    def load(self):
        source,images=message_content(message('before\n\n<AsyncImage query="sweet potato"/>\n\n| A | B |\n|---|---|\n| 1 | 2 |',metadata=component()))
        self.view.set_source(source,images=images,fetch=lambda descriptor,current:self.raw)
        end=time.monotonic()+3
        while time.monotonic()<end:
            self.root.update()
            if self.view.photos:
                return next(iter(self.view.media.values()))['loaded']
            time.sleep(.01)
        self.fail('image did not appear in the actual Tk renderer')

    def test_image_and_markdown_table_render_together(self):
        self.load();self.assertTrue(self.view.window_names());self.assertEqual(self.view.table_number,1)
        self.assertNotIn('AsyncImage',self.view.get('1.0','end'))
        self.assertIn('before',self.view.get('1.0','end'))

    def test_provider_without_image_loader_keeps_alt_text(self):
        self.view.set_source('![图片说明](https://example.com/image.png)')
        self.view.paint()
        self.assertIn('图片说明',self.view.get('1.0','end'))
        self.assertFalse(self.view.image_loader.pending())

    def test_image_can_be_saved_and_previewed(self):
        loaded=self.load()
        with TemporaryDirectory(prefix='desktop-ai-image-save-') as temporary:
            target=Path(temporary)/'saved.jpg'
            with patch('answer_view.filedialog.asksaveasfilename',return_value=str(target)):
                self.view.save_image(loaded)
            self.assertEqual(target.read_bytes(),self.raw)
        with patch('answer_view.monitor_at',return_value={'work':(15000,15000,16000,16000)}):
            self.view.show_preview(loaded);self.root.update()
        self.assertIsNotNone(self.view.preview.photo)
        self.view.close_preview();self.assertIsNone(self.view.preview)

    def test_loading_an_image_keeps_text_selection_intact(self):
        self.load();self.view.tag_add('sel','1.0','1.3')
        before=self.view.selected_text();self.view.rendered_width=0;self.view.paint()
        self.assertEqual(self.view.selected_text(),before)


if __name__=='__main__':
    unittest.main(verbosity=2)
