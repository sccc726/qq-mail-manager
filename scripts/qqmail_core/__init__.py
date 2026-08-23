"""Small shared primitives for the QQ Mail Manager CLI scripts.

This package deliberately contains only protocol-neutral M1 foundations.  It
does not change the existing scripts' sequence-number mail operations.
"""

from .config import Credentials, CredentialError, load_credentials
from .mailref import MailRef, MailRefError, parse_uid, parse_uidvalidity
from .results import EXIT_CODES, error_result, emit_json, result_exit_code

__all__ = [
    "Credentials", "CredentialError", "load_credentials",
    "MailRef", "MailRefError", "parse_uid", "parse_uidvalidity",
    "EXIT_CODES", "error_result", "emit_json", "result_exit_code",
]
