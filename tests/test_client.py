from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote_plus as standard_quote_plus
from urllib.parse import urlencode as standard_urlencode

import pytest
import requests
import responses

from thordata_serp.client import (
    DEFAULT_SCHEMA_ENDPOINT,
    DEFAULT_SERP_ENDPOINT,
    MAX_REQUEST_SIZE,
    MAX_RESPONSE_SIZE,
    RequestEncodingError,
    SchemaHttpClient,
    SerpClient,
)
from thordata_serp.response import ResponseError

SCHEMA_ENDPOINT = "https://schema.example.test/schema"
SERP_ENDPOINT = "https://serp.example.test/request"


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        chunks: list[bytes] | None = None,
        headers: Mapping[str, str] | None = None,
        iteration_error: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = dict(headers or {})
        self._chunks = chunks if chunks is not None else [b"{}"]
        self._iteration_error = iteration_error
        self.closed = False
        self.iterated = False

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        assert chunk_size == 64 * 1024
        self.iterated = True
        yield from self._chunks
        if self._iteration_error is not None:
            raise self._iteration_error

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeSession:
    response: FakeResponse | None = None
    error: Exception | None = None
    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._send("GET", url, kwargs)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._send("POST", url, kwargs)

    def _send(self, method: str, url: str, kwargs: dict[str, Any]) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def test_transport_constants_are_fixed() -> None:
    assert DEFAULT_SCHEMA_ENDPOINT == "https://api.thordata.com/serp/playground/schema?lang=en"
    assert DEFAULT_SERP_ENDPOINT == "https://scraperapi.thordata.com/request"
    assert MAX_REQUEST_SIZE == 1 << 20
    assert MAX_RESPONSE_SIZE == 10 << 20


@pytest.mark.parametrize("timeout", [0, -1, True, False, 1.5, "30", None])
@pytest.mark.parametrize("client_type", [SchemaHttpClient, SerpClient])
def test_clients_reject_invalid_timeout(client_type: type[Any], timeout: object) -> None:
    kwargs = {"api_key": "test-secret"} if client_type is SerpClient else {}
    with pytest.raises(ValueError, match="timeout"):
        client_type(timeout=timeout, **kwargs)


@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        "   ",
        "ftp://example.test/path",
        "example.test/path",
        "http://:80",
        "https://user@",
        "https://example.test:bad",
        "https://user@example.test/path",
        "https://user:pass@example.test/path",
        123,
        None,
    ],
)
@pytest.mark.parametrize("client_type", [SchemaHttpClient, SerpClient])
def test_clients_reject_invalid_private_endpoint(client_type: type[Any], endpoint: object) -> None:
    kwargs = {"api_key": "test-secret"} if client_type is SerpClient else {}
    with pytest.raises(ValueError) as captured:
        client_type(_endpoint=endpoint, **kwargs)

    assert str(captured.value) == "endpoint must be a nonblank http or https URL"


@pytest.mark.parametrize(
    "endpoint",
    [
        pytest.param("https:// example.test/path", id="leading-host-space"),
        pytest.param("https://example .test/path", id="internal-host-space"),
        pytest.param("https://example.test :443/path", id="trailing-host-space"),
        pytest.param("https://\texample.test/path", id="host-tab"),
        pytest.param("https://example.\ntest/path", id="host-newline"),
        pytest.param("https://example.\x00test/path", id="host-nul"),
        pytest.param("https://example.\u00a0test/path", id="host-unicode-space"),
    ],
)
@pytest.mark.parametrize("client_type", [SchemaHttpClient, SerpClient])
def test_clients_reject_whitespace_and_controls_in_authority(
    client_type: type[Any], endpoint: str
) -> None:
    kwargs = {"api_key": "test-secret"} if client_type is SerpClient else {}

    with pytest.raises(ValueError) as captured:
        client_type(_endpoint=endpoint, **kwargs)

    assert str(captured.value) == "endpoint must be a nonblank http or https URL"


@pytest.mark.parametrize("api_key", ["", "  ", 123, None, b"secret"])
def test_serp_client_rejects_invalid_api_key(api_key: object) -> None:
    with pytest.raises(ValueError, match="API key"):
        SerpClient(api_key)


def test_schema_fetch_uses_fixed_get_contract_and_returns_dict() -> None:
    response = FakeResponse(chunks=[b'{"data":', b'{"ok":true}}'])
    session = FakeSession(response=response)

    result = SchemaHttpClient(timeout=17, session=session, _endpoint=SCHEMA_ENDPOINT).fetch()

    assert result == {"data": {"ok": True}}
    assert session.calls == [
        (
            "GET",
            SCHEMA_ENDPOINT,
            {
                "headers": {"Accept": "application/json"},
                "timeout": 17,
                "stream": True,
                "allow_redirects": False,
            },
        )
    ]
    assert response.closed


@pytest.mark.parametrize("status", [199, 300, 401, 500])
def test_schema_fetch_rejects_non_2xx_before_reading(status: int) -> None:
    response = FakeResponse(status_code=status, chunks=[])
    session = FakeSession(response=response)

    with pytest.raises(ResponseError, match=str(status)):
        SchemaHttpClient(session=session, _endpoint=SCHEMA_ENDPOINT).fetch()

    assert not response.iterated
    assert response.closed


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"\xff", "UTF-8|JSON"),
        (b"not-json", "JSON"),
        (b"[]", "object"),
        (b'{"unsafe":NaN}', "JSON|finite"),
        (b'{"unsafe":"\\ud800"}', "Unicode|JSON"),
    ],
)
def test_schema_fetch_rejects_invalid_or_unsafe_json(body: bytes, message: str) -> None:
    response = FakeResponse(chunks=[body])

    with pytest.raises(ResponseError, match=message):
        SchemaHttpClient(session=FakeSession(response=response), _endpoint=SCHEMA_ENDPOINT).fetch()

    assert response.closed


def test_serp_request_uses_exact_post_contract_and_trims_key() -> None:
    response = FakeResponse(status_code=200, chunks=[b'{"ok":true}'])
    session = FakeSession(response=response)

    status, body = SerpClient(
        "  test-secret  ", timeout=23, session=session, _endpoint=SERP_ENDPOINT
    ).request({"q": "two words", "page": 2, b"raw": b"\xff"})

    assert (status, body) == (200, b'{"ok":true}')
    assert len(session.calls) == 1
    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert url == SERP_ENDPOINT
    assert kwargs["headers"] == {
        "Authorization": "Bearer test-secret",
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "Dify",
        "platform": "dify",
        "api-source": "plugin",
    }
    assert kwargs["data"] == "q=two+words&page=2&raw=%FF"
    assert isinstance(kwargs["auth"], requests.auth.AuthBase)
    assert kwargs["timeout"] == 23
    assert kwargs["stream"] is True
    assert kwargs["allow_redirects"] is False
    assert response.closed


@responses.activate
def test_bearer_auth_overrides_netrc_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        requests.sessions,
        "get_netrc_auth",
        lambda _url: ("netrc-user", "netrc-password"),
    )
    responses.post(SERP_ENDPOINT, status=200, body=b"ok")

    status, body = SerpClient("test-secret", _endpoint=SERP_ENDPOINT).request({"q": "x"})

    assert (status, body) == (200, b"ok")
    sent = responses.calls[0].request
    assert sent.headers["Authorization"] == "Bearer test-secret"
    assert sent.headers["Origin"] == "Dify"
    assert sent.headers["platform"] == "dify"
    assert sent.headers["api-source"] == "plugin"


def test_form_encoding_preserves_mapping_order_and_quote_plus_semantics() -> None:
    response = FakeResponse()
    session = FakeSession(response=response)

    SerpClient("test-secret", session=session, _endpoint=SERP_ENDPOINT).request(
        {"z": "a b", "q": "x/y?", "unicode": "\u20ac"}
    )

    assert session.calls[0][2]["data"] == "z=a+b&q=x%2Fy%3F&unicode=%E2%82%AC"


class BrokenStringValue:
    def __str__(self) -> str:
        raise RuntimeError("conversion failed")


def test_form_encoder_normalizes_safe_builtin_scalars() -> None:
    session = FakeSession(response=FakeResponse())

    SerpClient("test-secret", session=session, _endpoint=SERP_ENDPOINT).request(
        {"boolean": True, "integer": 7, "float": 1.5}
    )

    assert session.calls[0][2]["data"] == "boolean=True&integer=7&float=1.5"


@pytest.mark.parametrize(
    "form",
    [
        [("q", "x")],
        {"q": BrokenStringValue()},
        {"\ud800": "x"},
        {"q": "\ud800"},
    ],
)
def test_invalid_form_is_rejected_before_network(form: object) -> None:
    session = FakeSession(response=FakeResponse())

    with pytest.raises(RequestEncodingError, match="form|convert|Unicode|encode|mapping") as caught:
        SerpClient("test-secret", session=session, _endpoint=SERP_ENDPOINT).request(form)

    assert type(caught.value) is RequestEncodingError
    assert type(caught.value).__name__ == "RequestEncodingError"
    assert session.calls == []


class HostileString(str):
    def __iter__(self) -> Iterator[str]:
        return iter(())

    def encode(self, *args: object, **kwargs: object) -> bytes:
        return b"x" * (MAX_REQUEST_SIZE + 1)


class HostileBytes(bytes):
    def __iter__(self) -> Iterator[int]:
        return iter(())

    def decode(self, *args: object, **kwargs: object) -> str:
        return "x" * (MAX_REQUEST_SIZE + 1)


@pytest.mark.parametrize(
    "component",
    [HostileString("x"), HostileBytes(b"x")],
    ids=["str-subclass", "bytes-subclass"],
)
def test_unsafe_text_subclasses_cannot_bypass_request_limit(
    component: str | bytes,
) -> None:
    session = FakeSession(response=FakeResponse())

    with pytest.raises(RequestEncodingError, match="built-in scalar"):
        SerpClient("test-secret", session=session, _endpoint=SERP_ENDPOINT).request(
            {"q": component}
        )

    assert session.calls == []


class HugeStringValue:
    def __init__(self) -> None:
        self.called = False

    def __str__(self) -> str:
        self.called = True
        return "x" * (MAX_REQUEST_SIZE + 1)


def test_arbitrary_object_is_rejected_without_invoking_string_conversion() -> None:
    value = HugeStringValue()
    session = FakeSession(response=FakeResponse())

    with pytest.raises(RequestEncodingError, match="built-in scalar"):
        SerpClient("test-secret", session=session, _endpoint=SERP_ENDPOINT).request({"q": value})

    assert not value.called
    assert session.calls == []


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_float_is_rejected_before_network(value: float) -> None:
    session = FakeSession(response=FakeResponse())

    with pytest.raises(RequestEncodingError, match="finite"):
        SerpClient("test-secret", session=session, _endpoint=SERP_ENDPOINT).request({"q": value})

    assert session.calls == []


def test_actual_quoted_byte_size_is_rechecked_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from thordata_serp import client as client_module

    def expanding_quote_plus(value: str | bytes) -> str:
        if value == "x":
            return "\u20ac" * (MAX_REQUEST_SIZE // 3 + 1)
        return standard_quote_plus(value)

    monkeypatch.setattr(client_module, "quote_plus", expanding_quote_plus)
    session = FakeSession(response=FakeResponse())

    with pytest.raises(RequestEncodingError, match="large|limit|size"):
        SerpClient("test-secret", session=session, _endpoint=SERP_ENDPOINT).request({"q": "x"})

    assert session.calls == []


def test_huge_integer_is_rejected_by_precheck_before_network() -> None:
    value = 1 << (MAX_REQUEST_SIZE * 4)
    session = FakeSession(response=FakeResponse())

    with pytest.raises(RequestEncodingError, match="exceeds maximum size"):
        SerpClient("test-secret", session=session, _endpoint=SERP_ENDPOINT).request({"q": value})

    assert session.calls == []


def test_request_body_at_exact_limit_is_accepted() -> None:
    session = FakeSession(response=FakeResponse())

    SerpClient("test-secret", session=session, _endpoint=SERP_ENDPOINT).request(
        {"": "a" * (MAX_REQUEST_SIZE - 1)}
    )

    assert len(session.calls[0][2]["data"]) == MAX_REQUEST_SIZE


def test_request_body_over_limit_is_rejected_before_network() -> None:
    session = FakeSession(response=FakeResponse())

    with pytest.raises(RequestEncodingError, match="large|limit|size"):
        SerpClient("test-secret", session=session, _endpoint=SERP_ENDPOINT).request(
            {"": "a" * MAX_REQUEST_SIZE}
        )

    assert session.calls == []


@pytest.mark.parametrize(
    "component",
    [
        " " * 8 + "/" * (MAX_REQUEST_SIZE // 3),
        "\u20ac" * (MAX_REQUEST_SIZE // 9 + 1),
        b"\x00" * (MAX_REQUEST_SIZE // 3 + 1),
    ],
    ids=["spaces-and-ascii-escapables", "unicode", "bytes"],
)
def test_oversized_component_is_rejected_before_quoting(
    monkeypatch: pytest.MonkeyPatch, component: str | bytes
) -> None:
    from thordata_serp import client as client_module

    quoted_ids: list[int] = []

    def spy_quote_plus(value: str | bytes) -> str:
        quoted_ids.append(id(value))
        return standard_quote_plus(value)

    monkeypatch.setattr(client_module, "quote_plus", spy_quote_plus)
    session = FakeSession(response=FakeResponse())

    with pytest.raises(RequestEncodingError, match="large|limit|size"):
        SerpClient("test-secret", session=session, _endpoint=SERP_ENDPOINT).request(
            {"q": component}
        )

    assert id(component) not in quoted_ids
    assert session.calls == []


def test_later_oversized_value_is_rejected_before_quoting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from thordata_serp import client as client_module

    escaped_value = "\x00" * 4
    quoted: list[str | bytes] = []

    def spy_quote_plus(value: str | bytes) -> str:
        quoted.append(value)
        return standard_quote_plus(value)

    monkeypatch.setattr(client_module, "quote_plus", spy_quote_plus)
    session = FakeSession(response=FakeResponse())
    form = {"q": "a" * (MAX_REQUEST_SIZE - 16), "x": escaped_value}

    with pytest.raises(RequestEncodingError, match="large|limit|size"):
        SerpClient("test-secret", session=session, _endpoint=SERP_ENDPOINT).request(form)

    assert quoted == ["q", form["q"]]
    assert session.calls == []


def test_unicode_request_size_counts_percent_encoded_bytes() -> None:
    session = FakeSession(response=FakeResponse())
    prefix_size = len(b"q=")
    encoded_character_size = len(b"%E2%82%AC")
    accepted_count = (MAX_REQUEST_SIZE - prefix_size) // encoded_character_size

    SerpClient("test-secret", session=session, _endpoint=SERP_ENDPOINT).request(
        {"q": "\u20ac" * accepted_count}
    )

    assert len(session.calls[0][2]["data"]) <= MAX_REQUEST_SIZE


def test_unicode_request_body_at_exact_encoded_limit_is_accepted() -> None:
    session = FakeSession(response=FakeResponse())
    value_budget = MAX_REQUEST_SIZE - len("q=")
    encoded_character_size = len(standard_quote_plus("\u20ac"))
    unicode_count, ascii_count = divmod(value_budget, encoded_character_size)

    SerpClient("test-secret", session=session, _endpoint=SERP_ENDPOINT).request(
        {"q": "\u20ac" * unicode_count + "a" * ascii_count}
    )

    assert len(session.calls[0][2]["data"].encode("ascii")) == MAX_REQUEST_SIZE


def test_unicode_request_body_one_encoded_byte_over_limit_is_rejected() -> None:
    session = FakeSession(response=FakeResponse())
    value_budget = MAX_REQUEST_SIZE - len("q=")
    encoded_character_size = len(standard_quote_plus("\u20ac"))
    unicode_count, ascii_count = divmod(value_budget, encoded_character_size)
    value = "\u20ac" * unicode_count + "a" * (ascii_count + 1)

    with pytest.raises(RequestEncodingError, match="exceeds maximum size"):
        SerpClient("test-secret", session=session, _endpoint=SERP_ENDPOINT).request({"q": value})

    assert session.calls == []


@pytest.mark.parametrize(
    "form",
    [
        {"engine": "google", "q": "coffee and tea", "json": "1"},
        {"q": "\u5496\u5561", "country": "CN"},
        {"q": b"a b/\x00", "page": 1},
    ],
)
def test_form_encoding_matches_standard_urlencode(form: dict[object, object]) -> None:
    session = FakeSession(response=FakeResponse())

    SerpClient("test-secret", session=session, _endpoint=SERP_ENDPOINT).request(form)

    assert session.calls[0][2]["data"] == standard_urlencode(form)


def test_constructor_values_are_trimmed() -> None:
    schema = SchemaHttpClient(_endpoint=" https://schema.example.test/path ", timeout=3)
    serp = SerpClient(" test-secret ", _endpoint=" http://serp.example.test/path ", timeout=4)

    assert schema.endpoint == "https://schema.example.test/path"
    assert schema.timeout == 3
    assert serp.api_key == "test-secret"
    assert serp.endpoint == "http://serp.example.test/path"
    assert serp.timeout == 4


@responses.activate
def test_schema_does_not_follow_redirects() -> None:
    target = "https://schema.example.test/redirect-target"
    responses.get(SCHEMA_ENDPOINT, status=307, headers={"Location": target})
    responses.get(target, status=200, json={"unexpected": True})

    with pytest.raises(ResponseError, match="307"):
        SchemaHttpClient(_endpoint=SCHEMA_ENDPOINT).fetch()

    assert len(responses.calls) == 1


@responses.activate
def test_serp_does_not_follow_redirects() -> None:
    target = "https://serp.example.test/redirect-target"
    responses.post(SERP_ENDPOINT, status=307, headers={"Location": target}, body=b"redirect")
    responses.post(target, status=200, body=b"unexpected")

    result = SerpClient("test-secret", _endpoint=SERP_ENDPOINT).request({"q": "x"})

    assert result == (307, b"redirect")
    assert len(responses.calls) == 1


@pytest.mark.parametrize("client_kind", ["schema", "serp"])
@pytest.mark.parametrize("error_type", [requests.Timeout, requests.ConnectionError])
def test_request_exceptions_propagate(client_kind: str, error_type: type[Exception]) -> None:
    error = error_type("network failure")
    session = FakeSession(error=error)
    client: Any
    if client_kind == "schema":
        client = SchemaHttpClient(session=session, _endpoint=SCHEMA_ENDPOINT)
        invoke = client.fetch
    else:
        client = SerpClient("test-secret", session=session, _endpoint=SERP_ENDPOINT)

        def invoke() -> tuple[int, bytes]:
            return client.request({"q": "x"})

    with pytest.raises(error_type, match="network failure") as captured:
        invoke()

    assert captured.value is error


@pytest.mark.parametrize("client_kind", ["schema", "serp"])
def test_declared_response_over_limit_is_rejected_before_iteration(client_kind: str) -> None:
    response = FakeResponse(headers={"Content-Length": str(MAX_RESPONSE_SIZE + 1)})

    with pytest.raises(ResponseError, match="large|limit|size"):
        _invoke_client(client_kind, response)

    assert not response.iterated
    assert response.closed


@pytest.mark.parametrize("content_length", ["invalid", "-1", "+", "9" * 10_000])
@pytest.mark.parametrize("client_kind", ["schema", "serp"])
def test_malformed_or_pathological_content_length_is_streamed_safely(
    client_kind: str, content_length: str
) -> None:
    body = b"{}"
    response = FakeResponse(headers={"Content-Length": content_length}, chunks=[b"", body])

    result = _invoke_client(client_kind, response)

    expected = {} if client_kind == "schema" else (200, body)
    assert result == expected
    assert response.closed


@pytest.mark.parametrize("client_kind", ["schema", "serp"])
def test_streamed_response_at_exact_limit_is_accepted(client_kind: str) -> None:
    if client_kind == "schema":
        body = b'{"data":"' + b"a" * (MAX_RESPONSE_SIZE - len(b'{"data":""}')) + b'"}'
    else:
        body = b"a" * MAX_RESPONSE_SIZE
    response = FakeResponse(
        chunks=[body[: 64 * 1024], b"", body[64 * 1024 :]],
        headers={"Content-Length": str(MAX_RESPONSE_SIZE)},
    )

    result = _invoke_client(client_kind, response)

    if client_kind == "schema":
        assert isinstance(result, dict)
    else:
        assert result == (200, body)
    assert response.closed


@pytest.mark.parametrize("client_kind", ["schema", "serp"])
def test_streamed_response_over_limit_is_rejected(client_kind: str) -> None:
    response = FakeResponse(
        chunks=[b"a" * MAX_RESPONSE_SIZE, b"b"],
        headers={"Content-Length": "invalid"},
    )

    with pytest.raises(ResponseError, match="large|limit|size"):
        _invoke_client(client_kind, response)

    assert response.closed


@pytest.mark.parametrize("client_kind", ["schema", "serp"])
def test_response_closes_when_stream_iteration_fails(client_kind: str) -> None:
    response = FakeResponse(
        chunks=[b"{}"],
        iteration_error=requests.ConnectionError("stream failed"),
    )

    with pytest.raises(requests.ConnectionError, match="stream failed"):
        _invoke_client(client_kind, response)

    assert response.closed


def _invoke_client(client_kind: str, response: FakeResponse) -> object:
    session = FakeSession(response=response)
    if client_kind == "schema":
        return SchemaHttpClient(session=session, _endpoint=SCHEMA_ENDPOINT).fetch()
    return SerpClient("test-secret", session=session, _endpoint=SERP_ENDPOINT).request({"q": "x"})
