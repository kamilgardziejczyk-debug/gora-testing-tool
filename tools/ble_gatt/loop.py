"""A long-lived asyncio event loop on a background thread.

bleak is async-only, while the scenario wrappers are synchronous and a BLE
connection has to survive across several of their calls. A fresh
`asyncio.run()` per call cannot do that: a `BleakClient` is bound to the loop
that created it, so the connection would be torn down with the loop every time.
One background loop keeps every client on a single loop for its whole lifetime,
and lets the sync API block on each operation.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from typing import Any, Coroutine

LOGGER = logging.getLogger(__name__)

# Added on top of an operation's own timeout before giving up on the loop
# itself. bleak enforces the real timeout internally; this only catches a
# coroutine that never returns at all, so it stays generous.
GRACE_S = 5.0
SHUTDOWN_TIMEOUT_S = 5.0


class AsyncLoop:
    """Runs coroutines on a dedicated background event loop.

    Not reusable after `stop()`. One instance per BLE session.
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ble-asyncio", daemon=True)
        self._thread.start()
        self._ready.wait()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._ready.set)
        self._loop.run_forever()

    def run(self, coro: Coroutine, timeout_s: float | None = None) -> Any:
        """Submit `coro` to the loop and block until it finishes.

        On timeout the coroutine is cancelled rather than left running, so a
        stalled operation cannot keep touching the adapter while the next one
        starts. Raises `TimeoutError` in that case.
        """
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        budget_s = None if timeout_s is None else timeout_s + GRACE_S
        try:
            return future.result(budget_s)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(f"BLE operation did not finish within {budget_s}s") from None

    def stop(self) -> None:
        """Stop the loop and join its thread. Idempotent."""
        if self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=SHUTDOWN_TIMEOUT_S)
        if self._thread.is_alive():
            LOGGER.warning("BLE event loop thread did not stop within %ss", SHUTDOWN_TIMEOUT_S)
            return
        self._loop.close()
