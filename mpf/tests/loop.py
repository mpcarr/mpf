import selectors
from asyncio import base_events, futures, coroutine, events, test_utils
import collections
import heapq

# A class to manage set of next events:
from asyncio.selector_events import _SelectorSocketTransport
from unittest.mock import MagicMock


class NextTimers:
    def __init__(self):
        # Timers set. Used to check uniqueness:
        self._timers_set = set()
        # Timers heap. Used to get the closest timer event:
        self._timers_heap = []

    def add(self,when):
        """
        Add a timer (Future event).
        """
        # We don't add a time twice:
        if when in self._timers_set:
            return

        # Add to set:
        self._timers_set.add(when)
        # Add to heap:
        heapq.heappush(self._timers_heap,when)

    def is_empty(self):
        return (len(self._timers_set) == 0)

    def pop_closest(self):
        """
        Get closest event timer. (The one that will happen the soonest).
        """
        if self.is_empty():
            raise IndexError('NextTimers is empty')

        when = heapq.heappop(self._timers_heap)
        self._timers_set.remove(when)

        return when


class _TestTransport:
    def __init__(self, loop, sock):
        self._loop = loop
        self._sock = sock
        self._loop.add_reader(self._sock, self._read_ready)

    def _read_ready(self):
        pass

    def write(self, msg):
        pass

    def disconnect(self):
        pass


class MockSocket:
    def getsockname(self):
        return ""

    def getpeername(self):
        return ""

    def fileno(self):
        return self

    def close(self):
        return

    def read_ready(self):
        return False

    def write_ready(self):
        return False


class TestSelector(selectors.BaseSelector):
    def __init__(self):
        self.keys = {}

    def register(self, fileobj, events, data=None):
        key = selectors.SelectorKey(fileobj, 0, events, data)
        self.keys[fileobj] = key
        return key

    def unregister(self, fileobj):
        return self.keys.pop(fileobj)

    def select(self, timeout=None):
        del timeout
        ready = []
        for sock, key in self.keys.items():
            if sock.read_ready():
                ready.append((key, selectors.EVENT_READ))
            if sock.write_ready():
                ready.append((key, selectors.EVENT_WRITE))
        return ready

    def get_map(self):
        return self.keys


# Based on TestLoop from asyncio.test_utils:
class TimeTravelLoop(base_events.BaseEventLoop):
    """
    Loop for unittests. Passes time without waiting, but makes sure events
    happen in the correct order.
    """

    def __init__(self):
        self.readers = {}
        self.writers = {}

        super().__init__()

        self._time = 0
        self._clock_resolution = 1e-9
        self._timers = NextTimers()
        self._selector = TestSelector()
        self.reset_counters()

    def time(self):
        return self._time

    def set_time(self, time):
        """Set time in loop."""
        self._time = time

    def advance_time(self, advance):
        """Move test time forward."""
        if advance:
            self._time += advance

    def add_reader(self, fd, callback, *args):
        """Add a reader callback."""
        self._check_closed()
        handle = events.Handle(callback, args, self)
        try:
            key = self._selector.get_key(fd)
        except KeyError:
            self._selector.register(fd, selectors.EVENT_READ,
                                    (handle, None))
        else:
            mask, (reader, writer) = key.events, key.data
            self._selector.modify(fd, mask | selectors.EVENT_READ,
                                  (handle, writer))
            if reader is not None:
                reader.cancel()

    def remove_reader(self, fd):
        """Remove a reader callback."""
        if self.is_closed():
            return False
        try:
            key = self._selector.get_key(fd)
        except KeyError:
            return False
        else:
            mask, (reader, writer) = key.events, key.data
            mask &= ~selectors.EVENT_READ
            if not mask:
                self._selector.unregister(fd)
            else:
                self._selector.modify(fd, mask, (None, writer))

            if reader is not None:
                reader.cancel()
                return True
            else:
                return False

    def add_writer(self, fd, callback, *args):
        """Add a writer callback.."""
        self._check_closed()
        handle = events.Handle(callback, args, self)
        try:
            key = self._selector.get_key(fd)
        except KeyError:
            self._selector.register(fd, selectors.EVENT_WRITE,
                                    (None, handle))
        else:
            mask, (reader, writer) = key.events, key.data
            self._selector.modify(fd, mask | selectors.EVENT_WRITE,
                                  (reader, handle))
            if writer is not None:
                writer.cancel()

    def remove_writer(self, fd):
        """Remove a writer callback."""
        if self.is_closed():
            return False
        try:
            key = self._selector.get_key(fd)
        except KeyError:
            return False
        else:
            mask, (reader, writer) = key.events, key.data
            # Remove both writer and connector.
            mask &= ~selectors.EVENT_WRITE
            if not mask:
                self._selector.unregister(fd)
            else:
                self._selector.modify(fd, mask, (reader, None))

            if writer is not None:
                writer.cancel()
                return True
            else:
                return False

    def assert_writer(self, fd, callback, *args):
        assert fd in self.writers, 'fd {} is not registered'.format(fd)
        handle = self.writers[fd]
        assert handle[0] == callback, '{!r} != {!r}'.format(
            handle[0], callback)
        assert handle[1] == args, '{!r} != {!r}'.format(
            handle[1], args)

    def reset_counters(self):
        self.remove_reader_count = collections.defaultdict(int)
        self.remove_writer_count = collections.defaultdict(int)

#    def sock_connect(self, sock, address):
#        fut = futures.Future(loop=self)
#        fut.set_result(sock)
#        return fut

    @coroutine
    def create_connection(self, protocol_factory, host=None, port=None, *,
                          ssl=None, family=0, proto=0, flags=0, sock=None,
                          local_addr=None, server_hostname=None):
        sock = MockSocket()
        protocol = protocol_factory()
        return _SelectorSocketTransport(self, sock, protocol), protocol

#    def _make_socket_transport(self, sock, protocol, waiter=None, *, extra=None, server=None):
#        waiter.set_result(True)
#        return

    def _run_once(self):
        # Advance time only when we finished everything at the present:
        if len(self._ready) == 0:
            if not self._timers.is_empty():
                self._time = self._timers.pop_closest()

        super()._run_once()

    def call_at(self, when, callback, *args):
        self._timers.add(when)
        return super().call_at(when, callback, *args)

    def _process_events(self, event_list):
        for key, mask in event_list:
            fileobj, (reader, writer) = key.fileobj, key.data
            if mask & selectors.EVENT_READ and reader is not None:
                if reader._cancelled:
                    self.remove_reader(fileobj)
                else:
                    self._add_callback(reader)
            if mask & selectors.EVENT_WRITE and writer is not None:
                if writer._cancelled:
                    self.remove_writer(fileobj)
                else:
                    self._add_callback(writer)

    def _write_to_self(self):
        pass
