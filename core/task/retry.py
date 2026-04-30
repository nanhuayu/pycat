"""Retry logic with exponential backoff and error classification."""
from __future__ import annotations

import asyncio
import logging
import re
from enum import Enum
from typing import Optional

from core.task.types import RetryPolicy


logger = logging.getLogger(__name__)


class ErrorKind(str, Enum):
    TRANSIENT = "transient"        # network timeout, 5xx, connection reset
    RATE_LIMIT = "rate_limit"      # 429
    CONTEXT_OVERFLOW = "context_overflow"   # context window exceeded
    AUTH = "auth"                   # 401 / 403
    PERMANENT = "permanent"        # everything else


# Patterns applied case-insensitively against error message strings.
_RATE_PATTERNS = [re.compile(p, re.IGNORECASE) for p in [
    r"429", r"rate.?limit", r"too.?many.?requests", r"quota",
]]
_CONTEXT_PATTERNS = [re.compile(p, re.IGNORECASE) for p in [
    r"context.?window", r"context.?length", r"maximum.?context",
    r"token.?limit", r"too.?long", r"max_tokens",
]]
_AUTH_PATTERNS = [re.compile(p, re.IGNORECASE) for p in [
    r"401", r"403", r"unauthorized", r"forbidden", r"invalid.?api.?key",
    r"authentication",
]]
_TRANSIENT_PATTERNS = [re.compile(p, re.IGNORECASE) for p in [
    r"timeout", r"timed?\s*out", r"5\d{2}", r"connection.?(?:reset|refused|abort)",
    r"server.?error", r"internal.?server", r"bad.?gateway", r"service.?unavailable",
    r"ECONNRESET", r"ENOTFOUND",
    r"remote.?protocol.?error",
    r"server disconnected without sending (?:a )?response",
    r"connection.*closed", r"connection.*dropped", r"stream.*closed",
    r"unexpected.?eof", r"temporar(?:y|ily).*(?:disconnect|unavailable)",
]]


def classify_error(error: str | Exception) -> ErrorKind:
    """Classify an LLM/HTTP error into a retry category."""
    msg = str(error)
    for p in _RATE_PATTERNS:
        if p.search(msg):
            return ErrorKind.RATE_LIMIT
    for p in _CONTEXT_PATTERNS:
        if p.search(msg):
            return ErrorKind.CONTEXT_OVERFLOW
    for p in _AUTH_PATTERNS:
        if p.search(msg):
            return ErrorKind.AUTH
    for p in _TRANSIENT_PATTERNS:
        if p.search(msg):
            return ErrorKind.TRANSIENT
    return ErrorKind.PERMANENT


def is_retryable(kind: ErrorKind) -> bool:
    return kind in {ErrorKind.TRANSIENT, ErrorKind.RATE_LIMIT}


def compute_delay(policy: RetryPolicy, attempt: int, error_kind: ErrorKind) -> float:
    """Compute delay in seconds for a given retry attempt."""
    delay = policy.base_delay * (policy.backoff_factor ** attempt)
    # Rate-limit errors get longer initial delay.
    if error_kind == ErrorKind.RATE_LIMIT:
        delay = max(delay, 5.0)
    return min(delay, policy.max_delay)


async def retry_with_backoff(
    coro_factory,
    *,
    policy: RetryPolicy,
    on_retry: Optional[callable] = None,
):
    """Run an async callable with retry + exponential backoff.

    ``coro_factory`` is a zero-arg callable that returns a new awaitable each
    time (e.g. ``lambda: client.send(...)``).

    Returns the result on success.  Raises the last exception on exhaustion.
    """
    last_exc: Optional[Exception] = None

    for attempt in range(1 + policy.max_retries):
        try:
            return await coro_factory()
        except Exception as exc:
            last_exc = exc
            kind = classify_error(exc)

            if not is_retryable(kind) or attempt >= policy.max_retries:
                raise

            delay = compute_delay(policy, attempt, kind)
            if on_retry:
                try:
                    on_retry(attempt + 1, delay, str(exc))
                except Exception as callback_exc:
                    logger.debug("Retry callback failed on attempt %s: %s", attempt + 1, callback_exc)

            await asyncio.sleep(delay)

    # Should never reach here, but safety net.
    if last_exc:
        raise last_exc
