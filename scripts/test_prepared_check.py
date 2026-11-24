"""Deterministic scheduling, single-use, cancellation and send regressions."""
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from chatgpt_account import ChatGPTError
from chatgpt_client import Cancelled
from chatgpt_web_client import ChatGPTWebClient, CheckRejected
from prepared_check import PreparedCheck, PreparedCheckCache
from test_chatgpt_web import Isolated, Response, fixture, job, web_events


class Clock:
    def __init__(self):
        self.monotonic, self.wall = 100., 10000.

    def advance(self, seconds):
        self.monotonic += seconds
        self.wall += seconds


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.caches = []
        self.identity = b'first'
        self.generated = 0

    def tearDown(self):
        for cache in self.caches:
            cache.close()
        for cache in self.caches:
            if cache.thread:
                cache.thread.join(2)
                self.assertFalse(cache.thread.is_alive())

    def make_cache(self, generate=None, clock=None, interval=480):
        monotonic = (lambda: clock.monotonic) if clock else time.monotonic
        wall = (lambda: clock.wall) if clock else time.time
        def default_generate(current):
            self.generated += 1
            return PreparedCheck({'check':str(self.generated)}, self.identity, monotonic(), wall(), expires_after=540)
        cache = PreparedCheckCache(generate or default_generate, lambda:self.identity,
                                   interval=interval, monotonic=monotonic, wall=wall)
        self.caches.append(cache)
        return cache

    def wait(self, cache, predicate):
        with cache.condition:
            self.assertTrue(cache.condition.wait_for(predicate, timeout=2))

    def test_unused_check_refreshes_at_eight_minutes(self):
        clock = Clock();cache = self.make_cache(clock=clock)
        cache.start();self.wait(cache, lambda:cache.prepared is not None)
        clock.advance(479);cache.start()
        self.assertEqual(self.generated, 1)
        clock.advance(1);cache.start()
        self.wait(cache, lambda:cache.prepared is not None and cache.prepared.headers['check']=='2')
        self.assertEqual(cache.take(job())['check'], '2')

    def test_timer_refreshes_without_user_input(self):
        cache = self.make_cache(interval=.06)
        cache.start()
        self.wait(cache, lambda:cache.prepared is not None and int(cache.prepared.headers['check'])>=2)

    def test_take_is_single_use_and_refill_prepares_another(self):
        cache = self.make_cache();cache.start()
        first=cache.take(job())
        cache.refill()
        second=cache.take(job())
        self.assertNotEqual(first, second)

    def test_concurrent_takes_cannot_receive_the_same_check(self):
        cache = self.make_cache();cache.start()
        barrier = threading.Barrier(3);results=[]
        def take():
            barrier.wait();results.append(cache.take(job()))
        threads=[threading.Thread(target=take) for _ in range(2)]
        for thread in threads:thread.start()
        barrier.wait()
        for thread in threads:thread.join(2);self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 2)
        self.assertNotEqual(results[0], results[1])

    def test_identity_change_discards_old_ready_check(self):
        cache = self.make_cache();cache.start()
        self.wait(cache, lambda:cache.prepared is not None)
        self.identity=b'changed'
        self.assertEqual(cache.take(job())['check'], '2')

    def test_identity_change_during_generation_discards_result(self):
        started, release=threading.Event(),threading.Event()
        def generate(current):
            self.generated+=1;number=self.generated;identity=self.identity
            if number==1:started.set();release.wait(1)
            return PreparedCheck({'check':str(number)},identity,time.monotonic(),time.time(),expires_after=540)
        cache=self.make_cache(generate);cache.start();self.assertTrue(started.wait(1))
        self.identity=b'changed';release.set()
        self.assertEqual(cache.take(job())['check'], '2')

    def test_resume_wall_clock_jump_does_not_use_expired_check(self):
        clock=Clock();cache=self.make_cache(clock=clock);cache.start()
        self.wait(cache,lambda:cache.prepared is not None)
        clock.wall+=600
        self.assertEqual(cache.take(job())['check'],'2')

    def test_shorter_server_expiry_refreshes_before_eight_minutes(self):
        clock=Clock()
        def generate(current):
            self.generated+=1
            return PreparedCheck({'check':str(self.generated)},self.identity,clock.monotonic,clock.wall,
                                 expires_at=clock.wall+60,expires_after=60)
        cache=self.make_cache(generate,clock);cache.start()
        self.wait(cache,lambda:cache.prepared is not None)
        clock.advance(36);cache.start()
        self.wait(cache,lambda:cache.prepared is not None and cache.prepared.headers['check']=='2')

    def test_refresh_failure_keeps_old_unexpired_check(self):
        def generate(current):
            self.generated+=1
            if self.generated>1:raise ChatGPTError('offline')
            return PreparedCheck({'check':'original'},self.identity,time.monotonic(),time.time(),expires_after=540)
        cache=self.make_cache(generate,interval=.06);cache.start()
        self.wait(cache,lambda:cache.failure_serial>=2)
        self.assertEqual(cache.take(job())['check'],'original')

    def test_empty_cache_failure_is_reported_without_infinite_retry(self):
        def generate(current):
            self.generated+=1
            raise ChatGPTError('offline')
        cache=self.make_cache(generate);cache.start()
        self.wait(cache,lambda:cache.failure_serial==1)
        with self.assertRaisesRegex(ChatGPTError,'offline'):cache.take(job())
        self.assertEqual(self.generated,2)

    def test_cancel_waiter_preserves_background_work_for_next_question(self):
        release=threading.Event();started=threading.Event()
        def generate(current):
            started.set();release.wait(1)
            return PreparedCheck({'check':'one'},self.identity,time.monotonic(),time.time(),expires_after=540)
        cache=self.make_cache(generate);cache.start();self.assertTrue(started.wait(1))
        current=job();result=[]
        def take():
            try:cache.take(current)
            except Cancelled:result.append('cancelled')
        waiter=threading.Thread(target=take);waiter.start();current['cancel'].set()
        waiter.join(1);self.assertEqual(result,['cancelled'])
        release.set();self.assertEqual(cache.take(job())['check'],'one')

    def test_close_cancels_running_generation(self):
        started=threading.Event()
        def generate(current):
            started.set();current['cancel'].wait(1)
            raise Cancelled()
        cache=self.make_cache(generate);cache.start();self.assertTrue(started.wait(1))
        cache.close();cache.thread.join(1)
        self.assertFalse(cache.thread.is_alive());self.assertIsNone(cache.prepared)

    def test_forced_refresh_discards_an_already_running_generation(self):
        clock=Clock();started=threading.Event();release=threading.Event()
        def generate(current):
            self.generated+=1;number=self.generated
            if number==2:started.set();release.wait(1)
            return PreparedCheck({'check':str(number)},self.identity,clock.monotonic,clock.wall,expires_after=540)
        cache=self.make_cache(generate,clock);cache.start()
        self.wait(cache,lambda:cache.prepared is not None)
        clock.advance(480);cache.start();self.assertTrue(started.wait(1))
        result=[]
        waiter=threading.Thread(target=lambda:result.append(cache.take(job(),force_new=True)))
        waiter.start();self.wait(cache,lambda:cache.epoch>0);release.set();waiter.join(2)
        self.assertFalse(waiter.is_alive());self.assertEqual(result,[{'check':'3'}])


class ClientPrefetchTests(Isolated):
    def setUp(self):
        super().setUp()
        import chatgpt_web_account as account
        saved=fixture();account.save_account(saved,activate=True)
        self.client=ChatGPTWebClient(saved)
        self.generated=[]
        def generate(current):
            self.generated.append(current)
            current['check_expires_after']=540
            return {'test-check':str(len(self.generated))}
        self.patch=patch.object(self.client,'integrity',side_effect=generate)
        self.patch.start();self.client.start_preparation()
        self.wait_ready()

    def tearDown(self):
        self.client.close();self.client.checks.thread.join(2)
        self.assertFalse(self.client.checks.thread.is_alive())
        self.patch.stop();super().tearDown()

    def wait_ready(self):
        cache=self.client.checks
        with cache.condition:self.assertTrue(cache.condition.wait_for(lambda:cache.prepared is not None,2))

    def test_prepared_send_does_not_generate_in_foreground_and_refills(self):
        with patch.object(self.client,'request',return_value=Response(web_events(),stream=True)):
            self.client.run(job());self.wait_ready()
        self.assertEqual(list(self.client.events.queue)[-1]['type'],'complete')
        self.assertEqual(len(self.generated),2)
        self.assertTrue(all(current.get('_background_check') for current in self.generated))

    def test_rejected_prepared_check_is_replaced_and_sent_once_more(self):
        sent=[]
        def request(path,body=None,headers=None,**kwargs):
            sent.append((body,headers))
            if len(sent)==1:raise CheckRejected('rejected')
            return Response(web_events(),stream=True)
        with patch.object(self.client,'request',side_effect=request):
            self.client.run(job());self.wait_ready()
        self.assertEqual(len(sent),2)
        self.assertEqual(sent[0][0],sent[1][0])
        self.assertNotEqual(sent[0][1]['test-check'],sent[1][1]['test-check'])
        self.assertEqual(list(self.client.events.queue)[-1]['type'],'complete')

    def test_repeated_rejection_stops_after_two_submissions(self):
        with patch.object(self.client,'request',side_effect=CheckRejected('rejected')) as request:
            self.client.run(job())
        self.assertEqual(request.call_count,2)
        self.assertEqual(list(self.client.events.queue)[-1]['type'],'error')

    def test_ambiguous_network_error_never_replays_the_post(self):
        with patch.object(self.client,'request',side_effect=ChatGPTError('network interrupted')) as request:
            self.client.run(job())
        self.assertEqual(request.call_count,1)
        self.assertEqual(list(self.client.events.queue)[-1]['type'],'error')

    def test_expiry_metadata_is_retained_without_background_ui_events(self):
        self.client.close();self.client.checks.thread.join(2)
        self.patch.stop()
        current={**job(),'_background_check':True}
        with patch('chatgpt_web_client.solve',return_value=({'OpenAI-Sentinel-Chat-Requirements-Prepare-Token':'prepare'},
                    {'prepare_token':'prepare'})), patch.object(self.client,'json_request',return_value={
                    'token':'check','expire_after':540,'expire_at':time.time()+540}):
            self.client.integrity(current)
        self.assertEqual(current['check_expires_after'],540)
        self.assertTrue(self.client.events.empty())


if __name__=='__main__':
    unittest.main(verbosity=2)
