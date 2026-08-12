"""Shared retry decorator for HTTP clients talking to external services.

Retries only on transport-level failures where the request either never left
the machine or never completed — safe for both GETs and POSTs because these
error classes mean Graph / Gmail / TeamSnap / etc. never committed state on
their side. We deliberately do NOT retry on HTTP 5xx or generic ReadTimeout
for non-idempotent calls: a sendMail POST that returns 503 may actually have
succeeded, and a blind retry would double-send.
"""
import logging

import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

log = logging.getLogger(__name__)

_SAFE_RETRY_EXC = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.RemoteProtocolError,
)


def retry_http(func):
    """Retry a function up to 3 times on transport errors with exponential backoff."""
    return retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(_SAFE_RETRY_EXC),
        before_sleep=before_sleep_log(log, logging.WARNING),
        reraise=True,
    )(func)
