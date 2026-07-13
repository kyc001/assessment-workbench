from __future__ import annotations

import re

RETRYABLE_HTTP_STATUS_CODES = frozenset({429, 502, 503, 504, 524})


class RetryableWorkflowError(RuntimeError):
    pass


def is_retryable_http_status(status_code: int) -> bool:
    return status_code in RETRYABLE_HTTP_STATUS_CODES


def is_retryable_failure(error_code: str | None, error: str | None) -> bool:
    if error_code in {
        "RetryableWorkflowError",
        "TimeoutException",
        "NetworkError",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
    }:
        return True
    if error_code != "HTTPStatusError" or error is None:
        return False
    match = re.search(r"\b(429|502|503|504|524)\b", error)
    return match is not None and is_retryable_http_status(int(match.group(1)))
