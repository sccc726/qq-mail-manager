"""One authority for CLI result shape, JSON output, and exit codes."""
from __future__ import annotations

import json
import sys
from argparse import ArgumentParser
from collections.abc import Iterable, Mapping
from typing import Any, TextIO


EXIT_CODES = {"success": 0, "preview": 0, "partial": 2, "error": 1}
VALID_STATUSES = frozenset(EXIT_CODES)
RESERVED_RESULT_FIELDS = frozenset({
    "status", "message", "code", "success", "failed", "success_count",
    "failed_count", "total",
})


class ReservedResultFieldError(ValueError):
    """Raised when caller-supplied extra fields try to replace contract fields."""


class ArgumentParseError(ValueError):
    """A non-printing argparse failure that the CLI can convert to JSON."""


class StructuredArgumentParser(ArgumentParser):
    """Argument parser whose invalid input has a structured stdout result."""

    def error(self, message: str) -> None:
        raise ArgumentParseError(message)


def configure_cli_stdio() -> None:
    """Make real CLI help and JSON streams UTF-8 on every supported platform."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            # Imported/test streams may not expose a reconfigurable byte layer.
            pass


def _checked_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    protected = sorted(RESERVED_RESULT_FIELDS.intersection(fields))
    if protected:
        raise ReservedResultFieldError("保留结果字段不能被覆盖: " + ", ".join(protected))
    return dict(fields)


def error_result(*args: Any, code: str | None = None, **fields: Any) -> dict[str, Any]:
    """Build a non-secret structured error result."""
    extras = _checked_fields(fields)
    if len(args) != 1:
        raise TypeError("error_result 需要一个 message 位置参数")
    result: dict[str, Any] = {"status": "error", "message": str(args[0])}
    if code:
        result["code"] = code
    result.update(extras)
    return result


def batch_result(*, succeeded: Iterable[Any], failed: Iterable[Any], **fields: Any) -> dict[str, Any]:
    """Map a batch outcome to the contract's success/partial/error statuses."""
    extras = _checked_fields(fields)
    success_items, failed_items = list(succeeded), list(failed)
    status = "success" if not failed_items else "partial" if success_items else "error"
    result: dict[str, Any] = {
        "status": status,
        "success": success_items,
        "failed": failed_items,
        "success_count": len(success_items),
        "failed_count": len(failed_items),
        "total": len(success_items) + len(failed_items),
    }
    result.update(extras)
    return result


def argument_error_result(message: str) -> dict[str, Any]:
    """Map invalid CLI syntax to the normal single-document error protocol."""
    return error_result(f"参数错误: {message}", code="invalid_arguments")


def result_exit_code(result: Mapping[str, Any]) -> int:
    """Return the sole status-to-exit-code mapping used by migrated CLIs."""
    status = result.get("status")
    if status not in VALID_STATUSES:
        return EXIT_CODES["error"]
    return EXIT_CODES[status]


def emit_json(result: Mapping[str, Any], *, stream: TextIO | None = None) -> int:
    """Write exactly one JSON document to stdout and return its process code."""
    target = stream if stream is not None else sys.stdout
    target.write(json.dumps(dict(result), ensure_ascii=False, sort_keys=True) + "\n")
    return result_exit_code(result)
