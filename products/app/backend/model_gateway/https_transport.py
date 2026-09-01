"""Portable HTTPS transport for every OpenAI-compatible model provider."""

from __future__ import annotations

from collections.abc import Callable
import ssl
from typing import Any
from urllib.request import urlopen

import certifi


def bundled_ca_file() -> str:
    """Return the CA bundle shipped with the frozen App runtime."""

    return certifi.where()


def verified_ssl_context() -> ssl.SSLContext:
    """Build the provider-neutral trust context used by model HTTPS calls."""

    return ssl.create_default_context(cafile=bundled_ca_file())


def open_verified_https(
    request: Any,
    *,
    timeout: float,
    opener: Callable[..., Any] = urlopen,
) -> Any:
    """Open one HTTPS request without using the build machine's OpenSSL path.

    Injected openers remain untouched so provider tests and managed adapters do
    not need to understand ``ssl.SSLContext``.  Real urllib traffic always uses
    the certifi bundle that PyInstaller collects with the App.
    """

    if opener is not urlopen:
        return opener(request, timeout=timeout)
    context = verified_ssl_context()
    return opener(request, timeout=timeout, context=context)


__all__ = ["bundled_ca_file", "open_verified_https", "verified_ssl_context"]
