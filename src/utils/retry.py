"Retry strategies built on tenacity"

from __future__ import annotations

from tenacity import (
    retry,
    stop_after_attempt,
    wait_fixed,
    wait_exponential,
    retry_if_exception_type,
)

from src.schemas.task import RetryPolicy


def make_retry_decorator(policy: RetryPolicy, max_attempts: int = 3):
    """根据 RetryPolicy 创建 tenacity 装饰器参数"""
    if policy == RetryPolicy.ONCE:
        return retry(stop=stop_after_attempt(1))
    elif policy == RetryPolicy.LINEAR:
        return retry(stop=stop_after_attempt(max_attempts), wait=wait_fixed(2))
    elif policy == RetryPolicy.EXPONENTIAL:
        return retry(
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=1, min=1, max=30),
        )
    else:
        # ADAPTIVE: exponential with broader exception catching
        return retry(
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=1, min=1, max=30),
            retry=retry_if_exception_type(Exception),
        )
