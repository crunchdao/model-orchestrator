import asyncio
import itertools
import os
import signal
import sys

if sys.version_info >= (3, 12):
    batched = itertools.batched
else:
    def batched(iterable, n, *, strict=False):
        # batched('ABCDEFG', 3) → ABC DEF G
        if n < 1:
            raise ValueError('n must be at least one')
        iterator = iter(iterable)
        while batch := tuple(itertools.islice(iterator, n)):
            if strict and len(batch) != n:
                raise ValueError('batched(): incomplete batch')
            yield batch

if sys.version_info >= (3, 13):
    QueueShutDown = asyncio.queues.QueueShutDown
else:
    QueueShutDown = asyncio.CancelledError


def add_signal_handler(loop: asyncio.AbstractEventLoop, signalnum: signal.Signals, handler):

    def handler_wrapper(*args):
        handler()

    if os.name == 'nt':
        signal.signal(signalnum, handler_wrapper)
    else:
        loop.add_signal_handler(signalnum, handler_wrapper)
