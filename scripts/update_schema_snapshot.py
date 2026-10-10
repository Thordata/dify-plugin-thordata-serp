"""Fetch, validate, and atomically update the bundled SERP schema snapshot."""

import argparse
import errno
import json
import os
import stat
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

SOURCE_ROOT = str(Path(__file__).resolve().parents[1])
if SOURCE_ROOT not in sys.path:
    sys.path.insert(0, SOURCE_ROOT)

from thordata_serp.client import SchemaHttpClient  # noqa: E402
from thordata_serp.schema import validate_schema_payload  # noqa: E402

DEFAULT_TARGET = Path(__file__).resolve().parents[1] / "thordata_serp" / "serp-schema.snapshot.json"
_DIRECTORY_FSYNC_SUPPORTED = os.name == "posix"
_UNSUPPORTED_DIRECTORY_SYNC_ERRNOS = frozenset(
    error_number
    for error_number in (
        errno.EINVAL,
        getattr(errno, "ENOSYS", None),
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
    )
    if error_number is not None
)


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _engine_count(schema: dict[str, Any]) -> int:
    category_count = sum(
        len(category.get("engines", [])) for category in schema.get("categories", [])
    )
    return category_count + len(schema.get("engines", []))


def _summary_token(value: str, max_length: int = 128) -> str:
    if max_length < 0:
        raise ValueError("max_length must be nonnegative")

    parts: list[str] = []
    length = 0
    for character in value:
        if character == " ":
            encoded = "\\u0020"
        elif character == "\x7f":
            encoded = "\\u007f"
        else:
            encoded = json.dumps(character, ensure_ascii=True)[1:-1]
        if length + len(encoded) > max_length:
            marker = "..."[:max_length]
            limit = max_length - len(marker)
            while parts and length > limit:
                length -= len(parts.pop())
            return "".join(parts) + marker
        parts.append(encoded)
        length += len(encoded)
    return "".join(parts)


def _fsync_directory(directory: Path) -> None:
    if not _DIRECTORY_FSYNC_SUPPORTED:
        return

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError as error:
        if error.errno in _UNSUPPORTED_DIRECTORY_SYNC_ERRNOS:
            return
        raise

    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in _UNSUPPORTED_DIRECTORY_SYNC_ERRNOS:
                raise
    finally:
        os.close(descriptor)


def update_snapshot(client: Any, target: Path) -> dict[str, Any]:
    """Fetch and validate a schema before atomically replacing the target."""
    payload = client.fetch()
    schema = validate_schema_payload(payload)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    try:
        target_mode = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        target_mode = 0o644

    descriptor: int | None = None
    temporary: Path | None = None

    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f"{target.name}.", suffix=".tmp", dir=target.parent
        )
        temporary = Path(temporary_name)
        os.chmod(temporary, target_mode)

        handle = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
        descriptor = None
        with handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary, target)
        temporary = None
        _fsync_directory(target.parent)
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise

    return schema


def main(
    argv: list[str] | None = None,
    *,
    client_factory: Callable[[int], Any] = SchemaHttpClient,
) -> int:
    parser = argparse.ArgumentParser(
        description="Update the bundled Thordata SERP schema snapshot."
    )
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--timeout", type=_positive_integer, default=30)
    args = parser.parse_args(argv)

    try:
        schema = update_snapshot(client_factory(args.timeout), args.target)
    except Exception as error:
        print(f"error={_summary_token(type(error).__name__)}")
        return 1

    print(
        f"schema_version={_summary_token(schema['schema_version'])} engines={_engine_count(schema)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
