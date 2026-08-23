"""Shared server configuration and credential loading."""
from __future__ import annotations

import os
from dataclasses import dataclass
from collections.abc import Mapping


IMAP_HOST = "imap.qq.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 587
DEFAULT_TIMEOUT = 30.0
ENV_EMAIL = "QQ_EMAIL"
ENV_AUTH_CODE = "QQ_EMAIL_AUTH_CODE"


class CredentialError(ValueError):
    """Raised before any connection is made when required credentials are absent."""


@dataclass(frozen=True)
class Credentials:
    email: str
    auth_code: str


def load_credentials(environ: Mapping[str, str] | None = None) -> Credentials:
    """Read only the two supported QQ credential environment variables."""
    source = os.environ if environ is None else environ
    email, auth_code = source.get(ENV_EMAIL, ""), source.get(ENV_AUTH_CODE, "")
    if not email or not auth_code:
        raise CredentialError("缺少凭证信息，请先配置QQ邮箱地址和授权码")
    return Credentials(email=email, auth_code=auth_code)
