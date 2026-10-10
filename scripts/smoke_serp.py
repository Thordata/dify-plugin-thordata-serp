"""Run one explicit, low-volume Thordata SERP smoke request."""

import argparse
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

SOURCE_ROOT = str(Path(__file__).resolve().parents[1])
if SOURCE_ROOT not in sys.path:
    sys.path.insert(0, SOURCE_ROOT)

import requests  # noqa: E402

from thordata_serp.response import (  # noqa: E402
    ResponseError,
    SerpApiError,
    safe_error_message,
)
from thordata_serp.schema import SchemaError  # noqa: E402
from thordata_serp.serializer import ParameterError  # noqa: E402
from thordata_serp.service import build_service  # noqa: E402

DEFAULT_QUERY = "Dify Thordata integration smoke test"
_UNEXPECTED_ERROR = "Unable to complete Thordata SERP smoke test."
_EXPECTED_ERRORS = (
    SchemaError,
    ParameterError,
    ResponseError,
    SerpApiError,
    requests.RequestException,
)


def _result_size(data: object) -> int:
    return len(data) if isinstance(data, (dict, list)) else 0


def _summary_token(
    value: str,
    max_length: int = 128,
    *,
    sensitive_values: tuple[str, ...] = (),
) -> str:
    if max_length < 0:
        raise ValueError("max_length must be nonnegative")

    sensitive_values = tuple(
        sorted(
            (
                sensitive
                for sensitive in sensitive_values
                if type(sensitive) is str and 0 < len(sensitive) <= max_length
            ),
            key=len,
            reverse=True,
        )
    )
    parts: list[str] = []
    length = 0
    index = 0
    source_limit = min(len(value), max_length)
    while index < source_limit:
        replacement = next(
            (
                sensitive
                for sensitive in sensitive_values
                if index + len(sensitive) <= source_limit
                and value.startswith(sensitive, index, source_limit)
            ),
            None,
        )
        if replacement is None:
            characters = value[index]
            index += 1
        else:
            characters = "[REDACTED]"
            index += len(replacement)

        for character in characters:
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
    if index < len(value):
        marker = "..."[:max_length]
        limit = max_length - len(marker)
        while parts and length > limit:
            length -= len(parts.pop())
        return "".join(parts) + marker
    return "".join(parts)


def _require_success(value: object, stage: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("ok") is not True:
        raise ValueError(f"{stage} did not return a successful result")
    return value


def main(
    argv: list[str] | None = None,
    *,
    service_factory: Callable[[str], Any] = build_service,
) -> int:
    parser = argparse.ArgumentParser(description="Run an opt-in Thordata SERP smoke request.")
    parser.add_argument(
        "--api-key",
        default=os.environ.get("THORDATA_SERP_API_KEY", ""),
        help=(
            "API key override; command-line values may appear in process listings and shell "
            "history. Prefer the THORDATA_SERP_API_KEY environment variable."
        ),
    )
    parser.add_argument("--query", default=DEFAULT_QUERY)
    args = parser.parse_args(argv)

    api_key = args.api_key.strip() if isinstance(args.api_key, str) else ""
    if not api_key:
        print("THORDATA_SERP_API_KEY is required")
        return 2

    try:
        service = service_factory(api_key)
        engines = _require_success(service.list_engines(), "list_engines")
        default_engine = engines.get("default_engine")
        if not isinstance(default_engine, str) or not default_engine.strip():
            raise ValueError("default engine is missing")

        _require_success(service.get_engine_schema(default_engine), "get_engine_schema")
        result = _require_success(
            service.search(
                engine=default_engine,
                query=args.query,
                parameters_json='{"num":1}',
                response_format="1",
                response_mode="compact",
            ),
            "search",
        )

        schema_source = engines.get("schema_source")
        if schema_source not in {"remote", "cache", "snapshot"}:
            raise ValueError("schema source is invalid")
        status = result.get("status")
        if isinstance(status, bool) or not isinstance(status, int):
            raise ValueError("search status is invalid")

        print(
            f"ok=true schema_source={schema_source} "
            f"engine={_summary_token(default_engine, sensitive_values=(api_key, args.query))} "
            f"status={status} result_size={_result_size(result.get('data'))}"
        )
        return 0
    except _EXPECTED_ERRORS as error:
        print(f"ok=false error={safe_error_message(error, api_key)}")
        return 1
    except Exception:
        print(f"ok=false error={_UNEXPECTED_ERROR}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
