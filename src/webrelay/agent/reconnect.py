"""Reconnect backoff schedule for the local agent.

The single function exported here is consumed by
:meth:`webrelay.agent.client.RelayClient.run` to choose how long to sleep
between reconnect attempts after a socket drop. The schedule is
**exponential with full jitter** so a brief network blip recovers in
~1 s while a long outage backs off to the configured maximum.

The base sequence is ``initial, 2*initial, 4*initial, ..., max, max, max``
(every attempt after we reach the cap stays at ``max``). A fractional
jitter is applied to each yielded value, drawn uniformly from
``[base * (1 - jitter), base * (1 + jitter)]``, so two reconnecting
agents do not stampede the server in lockstep.

Note: this implementation is a regular (``def``) generator even though
``RelayClient.run`` consumes the values with ``await
asyncio.sleep(next(iter))``. That keeps the schedule trivially
deterministic in tests (callers may pass an explicit ``jitter_fn``) and
decouples the backoff math from the asyncio event loop.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Iterator


# A jitter function takes the two endpoints and returns a single float.
JitterFn = Callable[[float, float], float]


def reconnect_backoff(
    initial: float = 1.0,
    max: float = 60.0,  # noqa: A002 - matches stdlib "max" naming for limits
    jitter: float = 0.3,
    *,
    jitter_fn: JitterFn | None = None,
) -> Iterator[float]:
    """Yield sleep durations between reconnect attempts.

    The generator runs forever; the caller breaks out of the loop when
    reconnect succeeds (or when the agent is shutting down and the outer
    task is cancelled).

    Args:
        initial: The first sleep duration, in seconds. The first attempt
            is not preceded by a sleep — the loop yields ``initial``
            AFTER the first failed attempt.
        max: Cap on the sleep duration. The 60 s default keeps the
            phone's "Connected" badge from being permanently amber
            during a long Coolify outage.
        jitter: Fractional jitter in ``[0, 1)`` applied to every sleep
            duration. The yielded value is drawn uniformly from
            ``[base * (1 - jitter), base * (1 + jitter)]``. 0.3 (i.e.
            ±30 %) is the default recommended by the AWS post.
        jitter_fn: Optional callable used to draw the jitter value. It
            receives ``(low, high)`` and returns a float in that range
            (inclusive). Defaults to ``random.uniform``. Tests pass a
            deterministic function so the sequence is reproducible.

    Yields:
        Float seconds to sleep before the next reconnect attempt.

    Example::

        for delay in reconnect_backoff():
            try:
                await client.connect()
                break
            except (OSError, TimeoutError):
                await asyncio.sleep(delay)
    """
    if initial <= 0:
        raise ValueError("initial must be > 0")
    if max < initial:
        raise ValueError("max must be >= initial")
    if not 0.0 <= jitter < 1.0:
        raise ValueError("jitter must be in [0, 1)")

    draw = jitter_fn if jitter_fn is not None else random.uniform
    base = float(initial)
    cap = float(max)
    j = float(jitter)

    while True:
        low = base * (1.0 - j)
        high = base * (1.0 + j)
        # When jitter is 0 the interval collapses to a single point and
        # random.uniform would still return that point; guard against
        # low > high rounding edge cases anyway.
        if low > high:
            low, high = high, low
        yield draw(low, high)
        if base < cap:
            base = min(cap, base * 2.0)


__all__ = ["reconnect_backoff"]
