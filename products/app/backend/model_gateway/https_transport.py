"""Portable HTTPS transport for every OpenAI-compatible model provider."""

from __future__ import annotations

from collections.abc import Callable
import ssl
import socket
from functools import partial
from http.client import HTTPSConnection
from typing import Any
from urllib.request import urlopen, HTTPSHandler, build_opener

import certifi


def bundled_ca_file() -> str:
    """Return the CA bundle shipped with the frozen App runtime."""

    return certifi.where()


def verified_ssl_context() -> ssl.SSLContext:
    """Build the provider-neutral trust context used by model HTTPS calls."""

    return ssl.create_default_context(cafile=bundled_ca_file())


class _CancellableHTTPSConnection(HTTPSConnection):
    def __init__(self, *args: Any, request_control: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_control = request_control

    def getresponse(self):
        control = self._request_control
        control.raise_if_cancelled()
        connection = self.sock
        def interrupt(_reason):
            if connection is not None:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        remove = control.add_cancel_listener(interrupt)
        try:
            response = super().getresponse()
            if control.cancelled:
                response.close()
                control.raise_if_cancelled()
            return response
        except Exception:
            control.raise_if_cancelled()
            raise
        finally:
            remove()


class _CancellableHTTPSHandler(HTTPSHandler):
    def __init__(self, context: ssl.SSLContext, request_control: Any) -> None:
        super().__init__(context=context)
        self._request_control = request_control

    def https_open(self, request):
        return self.do_open(
            partial(_CancellableHTTPSConnection, request_control=self._request_control),
            request, context=self._context,
        )


def open_verified_https(
    request: Any,
    *,
    timeout: float,
    opener: Callable[..., Any] = urlopen,
    request_control: Any = None,
) -> Any:
    """Open one HTTPS request without using the build machine's OpenSSL path.

    Injected openers remain untouched so provider tests and managed adapters do
    not need to understand ``ssl.SSLContext``.  Real urllib traffic always uses
    the certifi bundle that PyInstaller collects with the App.
    """

    if opener is not urlopen:
        return opener(request, timeout=timeout)
    context = verified_ssl_context()
    if request_control is not None:
        request_control.raise_if_cancelled()
        return build_opener(_CancellableHTTPSHandler(context, request_control)).open(request, timeout=timeout)
    return opener(request, timeout=timeout, context=context)


__all__ = ["bundled_ca_file", "open_verified_https", "verified_ssl_context"]
