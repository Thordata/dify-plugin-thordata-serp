"""SERP response decoding, compaction, and secret redaction helpers."""

import json
from typing import Never

from .json_safety import validate_json_tree

_COMPACT_KEYS = frozenset(
    {"search_metadata", "search_parameters", "serpapi_pagination", "request_metadata"}
)
_BUSINESS_CODE_KEYS = ("code", "statusCode", "status_code", "error_code")
_MAX_RESPONSE_NESTING = 256
_MAX_ERROR_MESSAGE_CHARS = 512
_ERROR_FALLBACK = "Unexpected error while processing the SERP response."


class ResponseError(RuntimeError):
    """Raised when a SERP response cannot be decoded safely."""


class SerpApiError(RuntimeError):
    """Raised when Thordata or an upstream SERP provider reports an error."""

    def __init__(
        self,
        status_code: int,
        message: str,
        response_data: object | None = None,
    ) -> None:
        self.status_code = int(status_code)
        self.response_data = response_data
        super().__init__(message)


def _raise_response_error(
    message: str,
    error: Exception,
    secret: str | None,
    *,
    malformed_payload: bool = False,
) -> Never:
    response_error = ResponseError(message)
    response_error._malformed_payload = malformed_payload
    if secret:
        raise response_error from None
    raise response_error from error


def _clean_message(value: str, fallback: str, secret: str | None = None) -> str:
    if secret:
        value = value.replace(secret, "[REDACTED]")
        fallback = fallback.replace(secret, "[REDACTED]")
    message = " ".join(value.encode("utf-8", errors="replace").decode("utf-8").split())
    if not message:
        return fallback
    if len(message) > _MAX_ERROR_MESSAGE_CHARS:
        return message[: _MAX_ERROR_MESSAGE_CHARS - 3] + "..."
    return message


def safe_error_message(error: Exception, secret: str) -> str:
    """Render an exception without allowing string failures or secret disclosure."""
    try:
        message = str(error)
    except Exception:
        return _clean_message("", _ERROR_FALLBACK, secret)
    return _clean_message(message, _ERROR_FALLBACK, secret)


def _payload_message(payload: object, fallback: str, secret: str | None = None) -> str:
    if isinstance(payload, dict):
        for key in ("message", "error", "msg", "data"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return _clean_message(value, fallback, secret)
    if isinstance(payload, str) and payload.strip():
        return _clean_message(payload, fallback, secret)
    return _clean_message("", fallback, secret)


def _validate_response_tree(value: object, secret: str | None = None) -> None:
    try:
        validate_json_tree(value, max_depth=_MAX_RESPONSE_NESTING)
    except ValueError as error:
        validation_error = error
    else:
        return

    message = _clean_message(
        f"invalid JSON response: {validation_error}",
        "invalid JSON response",
        secret,
    )
    _raise_response_error(message, validation_error, secret)


def _load_json(body: bytes, secret: str | None = None) -> object:
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        RecursionError,
    ) as error:
        parse_error = error
    else:
        _validate_response_tree(parsed, secret)
        return parsed

    message = _clean_message("invalid JSON response", "invalid JSON response", secret)
    _raise_response_error(
        message,
        parse_error,
        secret,
        malformed_payload=isinstance(parse_error, (UnicodeDecodeError, json.JSONDecodeError)),
    )


def _plain_error_message(body: bytes, fallback: str, secret: str | None = None) -> str:
    try:
        message = body.decode("utf-8")
    except UnicodeDecodeError:
        return _clean_message("", fallback, secret)
    if message.lstrip().startswith("<"):
        return _clean_message("", fallback, secret)
    return _clean_message(message, fallback, secret)


def _unwrap_json(value: object, secret: str | None = None) -> object:
    current = value
    for _ in range(2):
        if not isinstance(current, str):
            break
        try:
            decoded = json.loads(current)
        except RecursionError as error:
            recursion_error = error
        except (json.JSONDecodeError, TypeError, ValueError):
            break
        else:
            _validate_response_tree(decoded, secret)
            if isinstance(decoded, (dict, list)):
                return decoded
            if isinstance(decoded, str):
                current = decoded
                continue
            break

        message = _clean_message(
            "invalid JSON response nesting too deep",
            "invalid JSON response",
            secret,
        )
        _raise_response_error(message, recursion_error, secret)
    return current


def _is_success_code(value: object) -> bool:
    if value is None:
        return True
    if type(value) is int:
        return value in (0, 200)
    if type(value) is str:
        return value in ("0", "200")
    return False


def _error_status(value: object) -> int:
    if type(value) is int:
        return value
    if type(value) is not str:
        return 500
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 500


def _sanitized_response_data(payload: object, secret: str | None) -> object:
    return redact(payload, secret or "")


def decode_serp_body(status_code: int, body: bytes, *, secret: str | None = None) -> object:
    """Decode an HTTP response and normalize transport and business errors."""
    if not 200 <= status_code < 300:
        fallback = f"SERP HTTP {status_code} error"
        try:
            parsed = _load_json(body, secret)
        except ResponseError as error:
            if getattr(error, "_malformed_payload", False):
                raise SerpApiError(
                    status_code,
                    _plain_error_message(body, fallback, secret),
                ) from None
            raise
        raise SerpApiError(
            status_code,
            _payload_message(parsed, fallback, secret),
            _sanitized_response_data(parsed, secret),
        )

    parsed = _load_json(body, secret)
    if isinstance(parsed, dict):
        for key in _BUSINESS_CODE_KEYS:
            if key in parsed and not _is_success_code(parsed[key]):
                error_status = _error_status(parsed[key])
                raise SerpApiError(
                    error_status,
                    _payload_message(parsed, f"SERP business error ({error_status})", secret),
                    _sanitized_response_data(parsed, secret),
                )

        error_value = parsed.get("error")
        if isinstance(error_value, str) and error_value.strip():
            raise SerpApiError(
                500,
                _clean_message(error_value, "SERP business error (500)", secret),
                _sanitized_response_data(parsed, secret),
            )
        result = parsed.get("data", parsed)
    else:
        result = parsed

    result = _unwrap_json(result, secret)
    if isinstance(result, str) and result.lstrip().lower().startswith(("error", "failed")):
        raise SerpApiError(
            500,
            _clean_message(result, "SERP business error (500)", secret),
            _sanitized_response_data(parsed, secret),
        )
    return result


def _copy_response(value: object, *, compact: bool, secret: str = "") -> object:
    _validate_response_tree(value, secret)
    if not isinstance(value, (dict, list)):
        return value.replace(secret, "[REDACTED]") if isinstance(value, str) and secret else value

    result: dict[str, object] | list[object] = {} if isinstance(value, dict) else []
    stack = [
        (
            value,
            result,
            iter(value.items()) if isinstance(value, dict) else iter(value),
        )
    ]

    while stack:
        source, target, iterator = stack[-1]
        try:
            item = next(iterator)
        except StopIteration:
            stack.pop()
            continue

        if isinstance(source, dict):
            key, item_value = item
            if compact and key in _COMPACT_KEYS:
                continue
            if secret:
                key = key.replace(secret, "[REDACTED]")
        else:
            key = None
            item_value = item

        if isinstance(item_value, (dict, list)):
            copied: dict[str, object] | list[object] = {} if isinstance(item_value, dict) else []
            if isinstance(target, dict):
                target[key] = copied
            else:
                target.append(copied)
            children = (
                iter(item_value.items()) if isinstance(item_value, dict) else iter(item_value)
            )
            stack.append((item_value, copied, children))
            continue

        if isinstance(item_value, str) and secret:
            item_value = item_value.replace(secret, "[REDACTED]")
        if isinstance(target, dict):
            target[key] = item_value
        else:
            target.append(item_value)

    return result


def compact_serp_response(value: object) -> object:
    """Return a copy with verbose SERP metadata removed at every level."""
    return _copy_response(value, compact=True)


def redact(value: object, secret: str) -> object:
    """Redact strings and keys, with the last value winning after key collisions."""
    return _copy_response(value, compact=False, secret=secret)
