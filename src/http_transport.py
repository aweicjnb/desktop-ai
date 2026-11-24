"""Bounded HTTPS keep-alive, retaining urllib's system proxy handling."""
import http.client
import io
import select
import socket
import ssl
import threading
import time
from urllib.error import URLError
from urllib.request import HTTPSHandler


_jobs_lock = threading.Lock()


def interrupted(job):
    return bool(job and (job['cancel'].is_set() or job.get('_abort_io')))


def track(job, connection, active):
    if job is not None:
        with _jobs_lock:
            connections = job.setdefault('_connections', set())
            connections.add(connection) if active else connections.discard(connection)


def abort_io(job):
    """Shutdown sockets first so close never blocks the Tk thread on a read."""
    with _jobs_lock:
        job['_abort_io'] = True
        connections = list(job.get('_connections', ()))
    for connection in connections:
        sock = connection.sock
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        threading.Thread(target=connection.close, daemon=True).start()


class _ResponseReader(io.RawIOBase):
    """SocketIO without its permanent poisoned state after a short timeout."""
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self._sock = owner
        # HTTPConnection may close its socket handle as soon as response
        # headers say "Connection: close". Keep the standard makefile ref
        # alive until the response body has finished reading.
        self._reference = owner.socket.makefile('rb', buffering=0)

    def readable(self):
        return True

    def close(self):
        if not self.closed:
            self._reference.close()
        super().close()

    def readinto(self, buffer):
        deadline = time.monotonic() + self.owner.read_timeout
        while True:
            if interrupted(self.owner.job):
                raise OSError('HTTPS read was stopped')
            try:
                return self.owner.socket.recv_into(buffer)
            except TimeoutError:
                if time.monotonic() >= deadline:
                    raise


class _JobSocket:
    """Poll cancellation during reads, including Windows' select-based waits."""
    def __init__(self, sock, job, read_timeout):
        self.socket = sock
        self.configure(job, read_timeout)

    def configure(self, job, read_timeout):
        self.job, self.read_timeout = job, read_timeout
        self.socket.settimeout(.25)

    def __getattr__(self, name):
        return getattr(self.socket, name)

    def sendall(self, data):
        if interrupted(self.job):
            raise OSError('HTTPS write was stopped')
        self.socket.settimeout(self.read_timeout)
        try:
            self.socket.sendall(data)
        finally:
            self.socket.settimeout(.25)

    def makefile(self, mode, buffering=None):
        if mode != 'rb':
            raise ValueError('Only binary HTTP response readers are supported')
        return io.BufferedReader(_ResponseReader(self))


class HTTPSPool:
    def __init__(self, maximum=4, idle_seconds=30):
        self.maximum, self.idle_seconds = maximum, idle_seconds
        self.lock = threading.Lock()
        self.idle, self.closed = [], False
        self.context = ssl.create_default_context()
        self.context.set_alpn_protocols(['http/1.1'])

    @staticmethod
    def usable(connection):
        sock = connection.sock
        if sock is None:
            return False
        try:
            # An idle socket readable before a new request generally means
            # EOF/unsolicited data. Discard it without replaying a POST.
            return not select.select([sock], [], [], 0)[0]
        except (OSError, ValueError):
            return False

    def take(self, key, factory):
        selected = None
        now = time.monotonic()
        with self.lock:
            if self.closed:
                raise URLError('HTTPS transport is closed')
            retained = []
            for item_key, connection, stored in self.idle:
                if now-stored >= self.idle_seconds or not self.usable(connection):
                    connection.close()
                elif selected is None and item_key == key:
                    selected = connection
                else:
                    retained.append((item_key, connection, stored))
            self.idle = retained
        return (selected, True) if selected else (factory(), False)

    def release(self, key, connection, reusable):
        with self.lock:
            if reusable and not self.closed and connection.sock is not None and len(self.idle) < self.maximum:
                if isinstance(connection.sock, _JobSocket):
                    connection.sock.job = None
                self.idle.append((key, connection, time.monotonic()))
            else:
                connection.close()

    def close(self):
        with self.lock:
            self.closed = True
            idle, self.idle = self.idle, []
        for _, connection, _ in idle:
            connection.close()


class PooledResponse:
    def __init__(self, response, connection, pool, key, job, reused):
        self.response, self.connection, self.pool = response, connection, pool
        self.key, self.job, self.reused = key, job, reused
        self._closed = False

    def __getattr__(self, name):
        return getattr(self.response, name)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __iter__(self):
        return self

    def __next__(self):
        line = self.response.readline()
        if not line:
            raise StopIteration
        return line

    def close(self):
        if self._closed:
            return
        self._closed = True
        consumed = self.response.isclosed() and not self.response.will_close
        self.response.close()
        # Make ownership transfer atomic with the stop path's socket snapshot.
        with _jobs_lock:
            if self.job is not None:
                self.job.get('_connections', set()).discard(self.connection)
            self.pool.release(self.key, self.connection, consumed and not interrupted(self.job))


class JobHTTPS(HTTPSHandler):
    def __init__(self, job, pool, timeout=40):
        super().__init__()
        self.job, self.pool, self.read_timeout = job, pool, timeout

    def https_open(self, request):
        job, pool = self.job, self.pool
        if interrupted(job):
            raise URLError('HTTPS operation was stopped')
        headers = {name.title(): value for name, value in {**request.unredirected_hdrs, **request.headers}.items()}
        headers['Connection'] = 'keep-alive'
        tunnel_headers = {}
        if request._tunnel_host and 'Proxy-Authorization' in headers:
            tunnel_headers['Proxy-Authorization'] = headers.pop('Proxy-Authorization')
        key = (request.host, request._tunnel_host, tuple(tunnel_headers.items()))

        def factory():
            connection = http.client.HTTPSConnection(request.host, timeout=min(request.timeout, 12), context=pool.context)
            if request._tunnel_host:
                connection.set_tunnel(request._tunnel_host, headers=tunnel_headers)
            return connection

        connection, reused = pool.take(key, factory)
        track(job, connection, True)
        try:
            if not connection.sock:
                connection.connect()
            if interrupted(job):
                raise URLError('HTTPS operation was stopped')
            if isinstance(connection.sock, _JobSocket):
                connection.sock.configure(job, self.read_timeout)
            else:
                connection.sock = _JobSocket(connection.sock, job, self.read_timeout)
            connection.request(request.get_method(), request.selector, request.data, headers,
                               encode_chunked=request.has_header('Transfer-encoding'))
            response = connection.getresponse()
            response.url, response.msg = request.get_full_url(), response.reason
            return PooledResponse(response, connection, pool, key, job, reused)
        except Exception:
            track(job, connection, False)
            connection.close()
            # Ambiguous requests are never automatically sent a second time.
            raise
