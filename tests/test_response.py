import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import thordata_serp  # noqa: E402
from thordata_serp.response import (
    ResponseError,
    SerpApiError,
    compact_serp_response,
    decode_serp_body,
    redact,
    safe_error_message,
)  # noqa: E402


def test_package_root_exports_only_deliberate_response_api() -> None:
    assert thordata_serp.__all__ == [
        "ResponseError",
        "SerpApiError",
        "compact_serp_response",
        "decode_serp_body",
        "redact",
        "safe_error_message",
    ]
    assert thordata_serp.ResponseError is ResponseError
    assert thordata_serp.SerpApiError is SerpApiError
    assert thordata_serp.compact_serp_response is compact_serp_response
    assert thordata_serp.decode_serp_body is decode_serp_body
    assert thordata_serp.redact is redact
    assert thordata_serp.safe_error_message is safe_error_message


def test_decodes_success_envelope_data_json_object() -> None:
    body = json.dumps({"code": 200, "data": json.dumps({"organic": [{"title": "Coffee"}]})})

    assert decode_serp_body(200, body.encode()) == {"organic": [{"title": "Coffee"}]}


def test_decodes_success_envelope_data_json_list() -> None:
    body = json.dumps({"data": json.dumps([{"title": "Coffee"}])})

    assert decode_serp_body(200, body.encode()) == [{"title": "Coffee"}]


def test_business_401_redacts_secret_from_error_and_response_data() -> None:
    secret = "synthetic-secret-123"
    body = json.dumps(
        {
            "code": 401,
            "message": f"invalid credential {secret}",
            secret: {"authorization": f"Bearer {secret}"},
        }
    ).encode()

    with pytest.raises(SerpApiError) as captured:
        decode_serp_body(200, body, secret=secret)

    assert captured.value.status_code == 401
    assert str(captured.value) == "invalid credential [REDACTED]"
    assert captured.value.response_data == {
        "code": 401,
        "message": "invalid credential [REDACTED]",
        "[REDACTED]": {"authorization": "Bearer [REDACTED]"},
    }
    assert secret not in repr(captured.value.response_data)


@pytest.mark.parametrize("key", ["code", "statusCode", "status_code", "error_code"])
@pytest.mark.parametrize(("value", "expected_status"), [(401, 401), ("429", 429), ("bad", 500)])
def test_business_error_code_keys_raise(key: str, value: object, expected_status: int) -> None:
    body = json.dumps({key: value, "message": "request denied"}).encode()

    with pytest.raises(SerpApiError) as captured:
        decode_serp_body(200, body)

    assert captured.value.status_code == expected_status
    assert str(captured.value) == "request denied"


@pytest.mark.parametrize("key", [None, "code", "statusCode", "status_code", "error_code"])
@pytest.mark.parametrize("value", [None, 0, "0", 200, "200"])
def test_accepted_business_success_codes(key: str | None, value: object) -> None:
    payload: dict[str, object] = {"data": {"organic": []}}
    if key is not None:
        payload[key] = value

    assert decode_serp_body(200, json.dumps(payload).encode()) == {"organic": []}


@pytest.mark.parametrize("value", ["", False, True, 0.0, 200.0, {"status": "bad"}])
def test_unsupported_business_codes_are_errors(value: object) -> None:
    body = json.dumps({"code": value, "message": "unsupported business code"}).encode()

    with pytest.raises(SerpApiError) as captured:
        decode_serp_body(200, body)

    assert captured.value.status_code == 500


def test_failure_code_is_not_masked_by_success_code() -> None:
    body = b'{"code":0,"statusCode":"401","message":"request denied"}'

    with pytest.raises(SerpApiError) as captured:
        decode_serp_body(200, body)

    assert captured.value.status_code == 401


@pytest.mark.parametrize("error", ["quota exhausted", "  quota exhausted  "])
def test_explicit_nonblank_error_string_raises(error: str) -> None:
    with pytest.raises(SerpApiError, match="quota exhausted") as captured:
        decode_serp_body(200, json.dumps({"error": error, "data": []}).encode())

    assert captured.value.status_code == 500


def test_blank_error_string_does_not_raise() -> None:
    assert decode_serp_body(200, b'{"error":"   ","data":[]}') == []


@pytest.mark.parametrize("body", [b"", b"not-json", b"\xff"])
def test_invalid_utf8_or_json_success_body_raises_response_error(body: bytes) -> None:
    with pytest.raises(ResponseError, match="invalid JSON"):
        decode_serp_body(200, body)


def test_response_error_message_redacts_secret() -> None:
    secret = "JSON"

    with pytest.raises(ResponseError) as captured:
        decode_serp_body(200, b"not-json", secret=secret)

    assert secret not in str(captured.value)
    assert "[REDACTED]" in str(captured.value)


def _exception_graph(root: BaseException) -> list[BaseException]:
    found: list[BaseException] = []
    pending = [root]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        found.append(current)
        for linked in (current.__cause__, current.__context__):
            if linked is not None:
                pending.append(linked)
    return found


@pytest.mark.parametrize(
    ("body", "secret"),
    [
        (b"not-json", "Expecting"),
        (b"\xff", "utf-8"),
        (b"1e999", "finite"),
    ],
)
def test_response_error_chain_does_not_leak_secret(body: bytes, secret: str) -> None:
    with pytest.raises(ResponseError) as captured:
        decode_serp_body(200, body, secret=secret)

    for error in _exception_graph(captured.value):
        assert secret not in str(error)
        assert secret not in repr(error)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"null", None),
        (b"true", True),
        (b"123", 123),
        (b'"ordinary result"', "ordinary result"),
        (b"[1,2]", [1, 2]),
        (b'{"item":1}', {"item": 1}),
    ],
)
def test_valid_scalar_list_and_object_json(body: bytes, expected: object) -> None:
    assert decode_serp_body(200, body) == expected


@pytest.mark.parametrize(
    "body",
    [b"NaN", b"Infinity", b"-Infinity", b"1e999", b'{"data":"NaN"}'],
)
def test_non_finite_numbers_raise_response_error(body: bytes) -> None:
    with pytest.raises(ResponseError, match="invalid JSON"):
        decode_serp_body(200, body)


def test_excessively_nested_json_raises_response_error() -> None:
    body = b"[" * 3000 + b"0" + b"]" * 3000

    with pytest.raises(ResponseError, match="invalid JSON|nesting"):
        decode_serp_body(200, body)


def test_excessively_nested_json_string_data_raises_response_error() -> None:
    nested_json = "[" * 3000 + "0" + "]" * 3000
    body = json.dumps({"data": nested_json}).encode()

    with pytest.raises(ResponseError, match="invalid JSON|nesting"):
        decode_serp_body(200, body)


def test_http_non_2xx_json_body_raises_with_sanitized_response_data() -> None:
    secret = "synthetic-secret-456"
    body = json.dumps({"message": f"upstream rejected {secret}", "token": secret}).encode()

    with pytest.raises(SerpApiError) as captured:
        decode_serp_body(503, body, secret=secret)

    assert captured.value.status_code == 503
    assert str(captured.value) == "upstream rejected [REDACTED]"
    assert captured.value.response_data == {
        "message": "upstream rejected [REDACTED]",
        "token": "[REDACTED]",
    }


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"bad gateway", "bad gateway"),
        (b"<html><body>bad gateway</body></html>", "SERP HTTP 502 error"),
        (b"\xff", "SERP HTTP 502 error"),
    ],
)
def test_http_non_2xx_plain_body_raises_safe_error(body: bytes, message: str) -> None:
    with pytest.raises(SerpApiError) as captured:
        decode_serp_body(502, body)

    assert captured.value.status_code == 502
    assert str(captured.value) == message
    assert captured.value.response_data is None


def test_http_error_fallback_redacts_secret() -> None:
    secret = "SERP"

    with pytest.raises(SerpApiError) as captured:
        decode_serp_body(502, b"{}", secret=secret)

    assert str(captured.value) == "[REDACTED] HTTP 502 error"
    assert secret not in str(captured.value)
    assert secret not in repr(captured.value.response_data)


def test_business_error_fallback_redacts_secret() -> None:
    secret = "SERP"

    with pytest.raises(SerpApiError) as captured:
        decode_serp_body(200, b'{"code":401}', secret=secret)

    assert str(captured.value) == "[REDACTED] business error (401)"
    assert secret not in str(captured.value)
    assert secret not in repr(captured.value.response_data)


@pytest.mark.parametrize("body", [b"NaN", b"Infinity", b"-Infinity", b"1e999"])
def test_unsafe_json_in_http_error_is_a_response_error(body: bytes) -> None:
    with pytest.raises(ResponseError):
        decode_serp_body(401, body)


def test_double_json_unwrapping_is_bounded() -> None:
    wrapped_object = json.dumps(json.dumps({"organic": []})).encode()
    too_deep = json.dumps(json.dumps(json.dumps(json.dumps({"organic": []})))).encode()

    assert decode_serp_body(200, wrapped_object) == {"organic": []}
    assert decode_serp_body(200, too_deep) == json.dumps({"organic": []})


@pytest.mark.parametrize("text", ["Error: invalid credential", " FAILED to reach provider"])
def test_plain_results_beginning_error_or_failed_raise(text: str) -> None:
    with pytest.raises(SerpApiError, match="(?i)error|failed"):
        decode_serp_body(200, json.dumps(text).encode())


def test_compact_removes_metadata_recursively_without_mutating_source() -> None:
    source = {
        "organic": [
            {
                "title": "Coffee",
                "search_metadata": {"id": "top"},
                "nested": [{"request_metadata": {"trace": "x"}, "keep": 1}],
            }
        ],
        "search_parameters": {"q": "coffee"},
        "serpapi_pagination": {"current": 1},
    }
    original = deepcopy(source)

    assert compact_serp_response(source) == {
        "organic": [{"title": "Coffee", "nested": [{"keep": 1}]}]
    }
    assert source == original


def test_redact_replaces_secret_in_nested_keys_and_values_without_mutation() -> None:
    secret = "synthetic-secret-789"
    source = {
        f"key-{secret}": f"Bearer {secret}",
        "nested": [{secret: secret}],
    }
    original = deepcopy(source)

    assert redact(source, secret) == {
        "key-[REDACTED]": "Bearer [REDACTED]",
        "nested": [{"[REDACTED]": "[REDACTED]"}],
    }
    assert source == original


def test_redaction_collision_uses_last_value_deterministically() -> None:
    source = {"synthetic-secret": 1, "[REDACTED]": 2}

    assert redact(source, "synthetic-secret") == {"[REDACTED]": 2}


def test_empty_secret_does_not_modify_response_strings() -> None:
    source = {"message": "ordinary response", "values": ["keep"]}

    assert redact(source, "") == source
    assert redact(source, "") is not source


def _deep_list() -> list[object]:
    root: list[object] = []
    current = root
    for _ in range(max(257, sys.getrecursionlimit() + 10)):
        nested: list[object] = []
        current.append(nested)
        current = nested
    return root


@pytest.mark.parametrize(
    "transform", [compact_serp_response, lambda value: redact(value, "secret")]
)
def test_transforms_reject_excessive_nesting(transform) -> None:
    with pytest.raises(ResponseError, match="nesting"):
        transform(_deep_list())


@pytest.mark.parametrize("container_type", ["dict", "list"])
@pytest.mark.parametrize(
    "transform", [compact_serp_response, lambda value: redact(value, "secret")]
)
def test_transforms_reject_cycles(transform, container_type: str) -> None:
    value: dict[str, object] | list[object] = {} if container_type == "dict" else []
    if isinstance(value, dict):
        value["self"] = value
    else:
        value.append(value)

    with pytest.raises(ResponseError, match="cycle"):
        transform(value)


@pytest.mark.parametrize("value", [{1: "value"}, {"nested": float("inf")}, {"x": object()}])
def test_transforms_reject_non_json_trees(value: object) -> None:
    with pytest.raises(ResponseError, match="invalid JSON"):
        redact(value, "secret")


def test_redact_validation_error_message_does_not_leak_secret() -> None:
    secret = "JSON"

    with pytest.raises(ResponseError) as captured:
        redact({"value": float("inf")}, secret)

    assert secret not in str(captured.value)
    assert "[REDACTED]" in str(captured.value)


class BrokenStringError(Exception):
    def __str__(self) -> str:
        raise RuntimeError("cannot render exception")


def test_safe_error_message_redacts_secret() -> None:
    assert safe_error_message(Exception("bad synthetic-secret"), "synthetic-secret") == (
        "bad [REDACTED]"
    )


def test_safe_error_message_falls_back_when_exception_string_fails() -> None:
    secret = "SERP"

    message = safe_error_message(BrokenStringError(), secret)

    assert message == "Unexpected error while processing the [REDACTED] response."
    assert secret not in message
