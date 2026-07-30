"""Bridge from Python's logging module into a `LogSession`."""

from __future__ import annotations

import logging

from .session import LogSession

# Matches the console format set up in main.py, minus the timestamp - the
# session stamps every line itself, so repeating it here would double it up.
TOOL_LOG_FORMAT = "%(levelname)s %(name)s: %(message)s"


class LogSessionHandler(logging.Handler):
    """Routes the tool's own log records into a `LogSession`.

    Attached alongside the console handler rather than replacing it, so the
    terminal still shows everything it did before while the same records also
    reach the tool and combined logs.
    """

    def __init__(self, session: LogSession):
        super().__init__()
        self.session = session
        self.setFormatter(logging.Formatter(TOOL_LOG_FORMAT))

    def emit(self, record: logging.LogRecord) -> None:
        """Write one formatted record to the session.

        A logging handler must never raise into the code that logged, so
        failures go to `handleError` (stderr) exactly as the stdlib handlers
        do - a broken log file should not take a scenario down with it.
        """
        try:
            self.session.write_tool(self.format(record))
        except Exception:  # noqa: BLE001 - handler contract: never propagate
            self.handleError(record)


def attach(session: LogSession) -> LogSessionHandler:
    """Start capturing all tool logging into `session`, returning the handler."""
    handler = LogSessionHandler(session)
    logging.getLogger().addHandler(handler)
    return handler


def detach(handler: LogSessionHandler) -> None:
    """Stop capturing tool logging through `handler`."""
    logging.getLogger().removeHandler(handler)
