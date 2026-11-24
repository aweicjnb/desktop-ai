"""Real local HTTP/1.1 tests for connection reuse, streaming and cancellation."""
from http.client import HTTPConnection, RemoteDisconnected
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from chatgpt_account import NoRedirect
from http_transport import HTTPSPool, JobHTTPS, abort_io


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.server.seen.append((self.path, self.client_address[1]))
        if self.path == '/stream':
            self.send_response(200)
            self.send_header('Content-Length', '30')
            self.end_headers()
            self.wfile.write(b'data: first\n\n');self.wfile.flush()
            self.server.finish_stream.wait(3)
            self.close_connection = True
            return
        self.send_response(403 if self.path == '/error' else 200)
        self.send_header('Content-Length', '6')
        if self.path == '/close':
            self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(b'abcdef');self.wfile.flush()

    def do_POST(self):
        self.server.seen.append((self.path, self.client_address[1]))
        self.rfile.read(int(self.headers.get('Content-Length', '0')))
        self.close_connection = True


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.server.seen, self.server.finish_stream = [], threading.Event()
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        # Exercise real HTTP/1.1 sockets locally; actual TLS/proxy behavior is
        # also checked against the user's normal HTTPS path during acceptance.
        self.connection_patch = patch('http_transport.http.client.HTTPSConnection',
            side_effect=lambda host, timeout, context: HTTPConnection(host, timeout=timeout))
        self.connection_patch.start()
        self.pool = HTTPSPool()
        self.job = {'cancel': threading.Event()}
        self.opener = build_opener(ProxyHandler({}), NoRedirect(), JobHTTPS(self.job, self.pool))

    def tearDown(self):
        self.job['cancel'].set()
        abort_io(self.job)
        self.pool.close()
        self.server.finish_stream.set()
        self.server.shutdown();self.server.server_close();self.thread.join(1)
        self.connection_patch.stop()

    def request(self, path, data=None):
        return self.opener.open(Request(f'https://127.0.0.1:{self.server.server_port}{path}', data=data), timeout=2)

    def test_fully_read_body_reuses_the_same_socket(self):
        with self.request('/first') as response:
            self.assertFalse(response.reused)
            self.assertEqual(response.read(), b'abcdef')
        with self.request('/second') as response:
            self.assertTrue(response.reused)
            self.assertEqual(response.read(), b'abcdef')
        self.assertEqual(self.server.seen[0][1], self.server.seen[1][1])

    def test_unread_body_is_not_reused(self):
        with self.request('/partial') as response:
            self.assertEqual(response.read(2), b'ab')
        with self.request('/next') as response:
            self.assertFalse(response.reused)
            response.read()

    def test_server_close_is_respected(self):
        with self.request('/close') as response:
            response.read()
        with self.request('/next') as response:
            self.assertFalse(response.reused)
            response.read()

    def test_http_error_remains_visible_and_read_body_can_be_reused(self):
        with self.assertRaises(HTTPError) as caught:
            self.request('/error')
        self.assertEqual(caught.exception.code, 403)
        self.assertEqual(caught.exception.read(), b'abcdef')
        caught.exception.close()
        with self.request('/next') as response:
            self.assertTrue(response.reused)
            response.read()

    def test_expired_idle_connection_is_discarded(self):
        self.pool.idle_seconds = .01
        with self.request('/first') as response:
            response.read()
        time.sleep(.02)
        with self.request('/next') as response:
            self.assertFalse(response.reused)
            response.read()

    def test_ambiguous_post_is_never_replayed(self):
        with self.assertRaises((RemoteDisconnected, URLError)):
            self.request('/disconnect', data=b'test')
        self.assertEqual([path for path, _ in self.server.seen], ['/disconnect'])

    def test_stream_delivers_first_line_then_cancel_unblocks_reader(self):
        response = self.request('/stream')
        self.assertEqual(next(response), b'data: first\n')
        self.assertEqual(next(response), b'\n')
        finished = threading.Event()
        def read():
            try:
                next(response)
            except (OSError, StopIteration, ValueError):
                pass
            finally:
                response.close();finished.set()
        worker = threading.Thread(target=read, daemon=True)
        worker.start()
        self.job['cancel'].set();abort_io(self.job)
        self.assertTrue(finished.wait(2), 'cancel must unblock a streaming read')
        worker.join(1)
        self.assertFalse(self.pool.idle)

    def test_closed_pool_rejects_new_requests(self):
        self.pool.close()
        with self.assertRaises(URLError):
            self.request('/next')

    def test_proxy_authorization_is_used_only_for_the_tunnel(self):
        request = Request('https://origin.invalid/path', headers={'Proxy-Authorization': 'Basic test'})
        request.set_proxy('proxy.invalid:8080', 'http')
        request.timeout = 2
        connection = Mock()
        response = connection.getresponse.return_value
        response.isclosed.return_value = True
        response.will_close = True
        with patch('http_transport.http.client.HTTPSConnection', return_value=connection):
            result = JobHTTPS(self.job, self.pool).https_open(request)
            result.close()
        connection.set_tunnel.assert_called_once_with('origin.invalid', headers={'Proxy-Authorization': 'Basic test'})
        sent_headers = connection.request.call_args.args[3]
        self.assertNotIn('Proxy-Authorization', sent_headers)


if __name__ == '__main__':
    unittest.main(verbosity=2)
