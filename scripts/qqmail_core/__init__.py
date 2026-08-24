"""Authoritative shared implementation for the seven QQ Mail CLI entrypoints.

Mail operations use stable folder + UIDVALIDITY + UID references; the thin
``scripts/*.py`` files only provide standalone-import bootstrapping and CLI
compatibility exports.
"""

from .config import Credentials, CredentialError, load_credentials
from .mailref import MailRef, MailRefError, parse_uid, parse_uidvalidity
from .results import EXIT_CODES, error_result, emit_json, result_exit_code

__all__ = [
    "Credentials", "CredentialError", "load_credentials",
    "MailRef", "MailRefError", "parse_uid", "parse_uidvalidity",
    "EXIT_CODES", "error_result", "emit_json", "result_exit_code",
]
