"""TLS-verified IMAP/SMTP connection lifecycle helpers."""
from __future__ import annotations

import imaplib
import smtplib
import ssl
from contextlib import contextmanager
from typing import Iterator

from .config import (DEFAULT_TIMEOUT, IMAP_HOST, IMAP_PORT, SMTP_HOST, SMTP_PORT,
                     Credentials)


def tls_context() -> ssl.SSLContext:
    """Create the standard-library default context with certificate checks enabled."""
    context = ssl.create_default_context()
    # These are explicit for auditability even though create_default_context sets them.
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    return context


def _safe_logout(client: object) -> None:
    try:
        client.logout()  # type: ignore[attr-defined]
    except Exception:
        pass


def _safe_smtp_close(client: object) -> None:
    try:
        client.quit()  # type: ignore[attr-defined]
    except Exception:
        try:
            client.close()  # type: ignore[attr-defined]
        except Exception:
            pass


@contextmanager
def imap_connection(credentials: Credentials, *, timeout: float = DEFAULT_TIMEOUT,
                    host: str = IMAP_HOST, port: int = IMAP_PORT) -> Iterator[imaplib.IMAP4_SSL]:
    """Authenticate an IMAP connection and log out on every exit path."""
    client = imaplib.IMAP4_SSL(host, port, ssl_context=tls_context(), timeout=timeout)
    try:
        client.login(credentials.email, credentials.auth_code)
        client._encoding = "utf-8"
        yield client
    finally:
        _safe_logout(client)


@contextmanager
def smtp_connection(credentials: Credentials, *, timeout: float = DEFAULT_TIMEOUT,
                    host: str = SMTP_HOST, port: int = SMTP_PORT) -> Iterator[smtplib.SMTP]:
    """Authenticate a STARTTLS SMTP connection and close it on every exit path."""
    client = smtplib.SMTP(host, port, timeout=timeout)
    try:
        client.ehlo()
        client.starttls(context=tls_context())
        client.ehlo()
        client.login(credentials.email, credentials.auth_code)
        yield client
    finally:
        _safe_smtp_close(client)
