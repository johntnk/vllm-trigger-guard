"""Single-event-loop admission and output budgets for the isolated adapter."""
import asyncio
from collections import deque
from dataclasses import dataclass


class Overloaded(RuntimeError):
    pass


class TransportFailure(RuntimeError):
    pass


@dataclass(eq=False)
class Lease:
    tokens: int
    input_bytes: int
    ready: asyncio.Future
    admitted: bool = False
    released: bool = False


class Admission:
    def __init__(self, engine_limit=16, stream_limit=64, waiting_limit=16,
                 waiting_tokens=65536, waiting_bytes=1048576, inflight_tokens=131072):
        self.engine_limit, self.stream_limit, self.waiting_limit = engine_limit,stream_limit,waiting_limit
        self.waiting_token_limit, self.waiting_byte_limit = waiting_tokens,waiting_bytes
        self.inflight_token_limit = inflight_tokens
        self.streams = self.inflight = self.inflight_tokens = 0
        self.waiting_tokens = self.waiting_bytes = 0
        self.waiters=deque()
        self.failed=False
        self.rejected=0

    def open_stream(self):
        if self.failed or self.streams >= self.stream_limit:
            self.rejected+=1
            raise Overloaded('stream_budget' if not self.failed else 'cleanup_failed')
        self.streams+=1

    def close_stream(self):
        self.streams-=1
        assert self.streams>=0

    def _can_admit(self, item):
        return self.inflight<self.engine_limit and self.inflight_tokens+item.tokens<=self.inflight_token_limit

    def _admit(self,item):
        item.admitted=True
        self.inflight+=1
        self.inflight_tokens+=item.tokens
        item.ready.set_result(None)

    def _unqueue(self,item):
        self.waiters.remove(item)
        self.waiting_tokens-=item.tokens
        self.waiting_bytes-=item.input_bytes

    def _wake(self):
        while self.waiters and not self.failed and self._can_admit(self.waiters[0]):
            item=self.waiters[0]
            self._unqueue(item)
            if not item.ready.cancelled():
                self._admit(item)

    async def acquire(self,tokens,input_bytes,timeout):
        item=Lease(tokens,input_bytes,asyncio.get_running_loop().create_future())
        if self.failed or tokens>self.inflight_token_limit:
            self.rejected+=1
            raise Overloaded('engine_token_budget')
        if not self.waiters and self._can_admit(item):
            self._admit(item)
        else:
            if (len(self.waiters)>=self.waiting_limit or
                    self.waiting_tokens+tokens>self.waiting_token_limit or
                    self.waiting_bytes+input_bytes>self.waiting_byte_limit):
                self.rejected+=1
                raise Overloaded('waiting_budget')
            self.waiters.append(item)
            self.waiting_tokens+=tokens;self.waiting_bytes+=input_bytes
        try:
            # Python 3.11 wait_for can swallow cancellation if ready completes
            # in the same loop turn. Keep cancellation on the acquiring task
            # so the cleanup below always returns its queue/engine budget.
            async with asyncio.timeout(timeout):
                await asyncio.shield(item.ready)
            return item
        except BaseException:
            if item in self.waiters:
                self._unqueue(item)
                item.ready.cancel()
                self._wake()
            elif item.admitted:
                self.release_engine(item)
            raise

    def release_engine(self,item):
        if item.released:
            return
        assert item.admitted
        item.released=True
        self.inflight-=1;self.inflight_tokens-=item.tokens
        assert self.inflight>=0 and self.inflight_tokens>=0
        self._wake()

    def snapshot(self):
        return dict(streams=self.streams,engine_inflight=self.inflight,
            engine_tokens=self.inflight_tokens,waiting=len(self.waiters),
            waiting_tokens=self.waiting_tokens,waiting_bytes=self.waiting_bytes,
            rejected=self.rejected,failed=self.failed)


class ByteBudget:
    def __init__(self,limit):
        self.limit=limit;self.used=0;self.peak=0

    def reserve(self,size):
        if self.used+size>self.limit:
            return False
        self.used+=size;self.peak=max(self.peak,self.used)
        return True

    def release(self,size):
        self.used-=size
        assert self.used>=0


class OutputBuffer:
    """Nonblocking producer; separate reserved compact terminal descriptor.

    Payload remains charged while the sender owns it, until ASGI send completes.
    Failure intentionally closes the stream; discarded bytes are not success.
    """
    def __init__(self,budget,limit):
        self.budget=budget;self.limit=limit;self.pending=0
        self.items=deque();self.ready=asyncio.Event()
        self.terminal=None;self.error=None;self.closed=False
        self.sending_cost=0

    def put(self,payload):
        if self.closed or self.terminal is not None or self.error is not None:
            return False
        cost=len(payload)*2+256
        if self.pending+cost>self.limit or not self.budget.reserve(cost):
            self.fail(TransportFailure('output_byte_budget'))
            raise self.error
        self.pending+=cost
        self.items.append((payload,cost));self.ready.set()
        return True

    def finish(self,descriptor):
        if self.terminal is None and self.error is None and not self.closed:
            self.terminal=descriptor;self.ready.set()

    def fail(self,error):
        if self.error is None:
            self.error=error
        self.ready.set()

    async def next(self):
        while not self.items and self.terminal is None and self.error is None:
            self.ready.clear()
            await self.ready.wait()
        if self.error is not None:
            raise self.error
        if self.items:
            payload,cost=self.items.popleft()
            self.sending_cost=cost
            return payload
        return None

    def sent(self):
        self.pending-=self.sending_cost
        self.budget.release(self.sending_cost)
        self.sending_cost=0

    def close(self):
        if not self.closed:
            self.closed=True
            self.items.clear()
            self.budget.release(self.pending)
            self.pending=0;self.sending_cost=0
            self.ready.set()
