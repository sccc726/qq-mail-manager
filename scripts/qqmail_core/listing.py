#!/usr/bin/env python3
"""List QQ Mail folders using the M1 shared connection and folder core."""
from __future__ import annotations

from .config import CredentialError, Credentials, load_credentials
from .connections import imap_connection
from .folders import FolderError, parse_list_response
from .results import (ArgumentParseError, StructuredArgumentParser,
                                 argument_error_result, emit_json, error_result)


def list_folders(email_addr: str, auth_code: str):
    """List folders while preserving the M0 public result fields."""
    try:
        with imap_connection(Credentials(email_addr, auth_code)) as mail:
            status, rows = mail.list()
            if status != "OK":
                return error_result("无法获取文件夹列表", code="imap_list_failed")
            folders = [parse_list_response(row).public_dict() for row in (rows or [])]
        return {"status": "success", "folders": folders, "total": len(folders)}
    except FolderError as exc:
        return error_result(f"文件夹列表解析失败: {exc}", code="folder_parse_failed")
    except Exception:
        # Do not reflect arbitrary connection/library errors: they may contain
        # server diagnostics and must never expose an authorization code.
        return error_result("IMAP连接或认证失败", code="imap_error")


def main():
    parser = StructuredArgumentParser(description="列出QQ邮箱所有文件夹")
    try:
        parser.parse_args()
    except ArgumentParseError as exc:
        return emit_json(argument_error_result(str(exc)))
    try:
        credentials = load_credentials()
    except CredentialError as exc:
        return emit_json(error_result(str(exc), code="missing_credentials"))
    return emit_json(list_folders(credentials.email, credentials.auth_code))


if __name__ == "__main__":
    raise SystemExit(main())
