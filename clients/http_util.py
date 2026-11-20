"""Shared HTTP client settings. Never logs authorization headers or API keys."""

from __future__ import annotations

import httpx

DEFAULT_TIMEOUT = httpx.Timeout(25.0, connect=10.0)
USER_AGENT = "CHS-Provider-Status-API/1.0"


def default_headers() -> dict[str, str]:
    return {
        "Accept": "application/fhir+json, application/json",
        "User-Agent": USER_AGENT,
    }


def new_async_client(**kwargs) -> httpx.AsyncClient:
    timeout = kwargs.pop("timeout", DEFAULT_TIMEOUT)
    headers = default_headers()
    extra = kwargs.pop("headers", None)
    if extra:
        headers.update(extra)
    return httpx.AsyncClient(
        timeout=timeout,
        headers=headers,
        follow_redirects=True,
        **kwargs,
    )
