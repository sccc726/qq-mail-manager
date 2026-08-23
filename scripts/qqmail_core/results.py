"""One authority for CLI result shape, JSON output, and exit codes."""
from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Mapping
from typing import Any, TextIO


EXIT_CODES = {"success": 0, "preview": 0, "partial": 2, "error": 1}
VALID_STATUSES = frozenset(EXIT_CODES)


def error_result(message: str, *, code: str | None = None, **fields: Any) -> dict[str, Any]:
    """Build a non-secret structured error result."""
    result: dict[str, Any] = {"status": "error", "message": str(message)}
    if code:
        result["code"] = code
    result.update(fields)
    return result


def batch_result(*, succeeded: Iterable[Any], failed: Iterable[Any], **fields: Any) -> dict[str, Any]:
    """Map a batch outcome to the contract's success/partial/error statuses."""
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
    result.update(fields)
    return result


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
