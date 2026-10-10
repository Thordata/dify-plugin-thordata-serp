from typing import Any
from urllib.parse import parse_qs

import pytest
import requests
import responses
from dify_plugin.errors.tool import ToolProviderCredentialValidationError

from provider import thordata_serp as provider_module
from provider.thordata_serp import ThordataSerpProvider
from thordata_serp import service as service_module
from thordata_serp.client import DEFAULT_SCHEMA_ENDPOINT, DEFAULT_SERP_ENDPOINT
from thordata_serp.response import ResponseError, SerpApiError, safe_error_message
from thordata_serp.schema import SchemaError
from thordata_serp.serializer import ParameterError

API_KEY_ERROR = "A Thordata SERP API key is required."
UNEXPECTED_ERROR = "Unable to validate Thordata SERP credentials."
EXPECTED_SEARCH = {
    "engine": "google",
    "query": "Dify credential validation",
    "parameters_json": '{"num":1}',
    "response_format": "1",
    "response_mode": "compact",
}


class RecordingService:
    def __init__(self, result: object = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def search(self, **parameters: Any) -> object:
        self.calls.append(parameters)
        if self.error is not None:
            raise self.error
        return self.result


class StringSubclass(str):
    pass


class BrokenStringError(RuntimeError):
    def __str__(self) -> str:
        raise RuntimeError("string conversion failed")


def make_provider() -> ThordataSerpProvider:
    return object.__new__(ThordataSerpProvider)


def exception_graph(root: BaseException) -> list[BaseException]:
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


def assert_secret_free_failure(error: BaseException, secret: str) -> None:
    chain = exception_graph(error)
    assert chain == [error]
    for item in chain:
        assert secret not in str(item)
        assert secret not in repr(item)


def test_provider_validates_with_exact_minimal_search_and_ignores_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response_payload = {"credential": "synthetic-key", "unexpected": object()}
    service = RecordingService(result=response_payload)
    built_with: list[str] = []

    def fake_build_service(api_key: str) -> RecordingService:
        built_with.append(api_key)
        return service

    monkeypatch.setattr(provider_module, "build_service", fake_build_service, raising=False)

    result = make_provider()._validate_credentials({"api_key": "  synthetic-key  "})

    assert result is None
    assert built_with == ["synthetic-key"]
    assert service.calls == [EXPECTED_SEARCH]


@responses.activate
def test_provider_validates_through_real_service_and_http_clients(
    monkeypatch: pytest.MonkeyPatch,
    schema_payload: dict[str, Any],
) -> None:
    monkeypatch.setattr(service_module, "_shared_schema_repository", None)
    responses.get(DEFAULT_SCHEMA_ENDPOINT, json=schema_payload, status=200)
    responses.post(
        DEFAULT_SERP_ENDPOINT,
        json={"code": 0, "data": {"organic": []}},
        status=200,
    )

    result = make_provider()._validate_credentials({"api_key": "  synthetic-key  "})

    assert result is None
    assert len(responses.calls) == 2
    schema_calls = [call for call in responses.calls if call.request.method == "GET"]
    search_calls = [call for call in responses.calls if call.request.method == "POST"]
    assert len(schema_calls) == 1
    assert len(search_calls) == 1
    schema_request = schema_calls[0].request
    search_request = search_calls[0].request
    assert schema_request.method == "GET"
    assert schema_request.url == DEFAULT_SCHEMA_ENDPOINT
    assert search_request.method == "POST"
    assert search_request.url == DEFAULT_SERP_ENDPOINT
    assert search_request.headers["Authorization"] == "Bearer synthetic-key"
    body = search_request.body
    assert isinstance(body, bytes | str)
    if isinstance(body, bytes):
        body = body.decode("ascii")
    assert parse_qs(body) == {
        "engine": ["google"],
        "q": ["Dify credential validation"],
        "num": ["1"],
        "json": ["1"],
    }


@pytest.mark.parametrize(
    "credentials",
    [
        {},
        {"api_key": None},
        {"api_key": 123},
        {"api_key": "\t \r\n"},
        {"api_key": StringSubclass("synthetic-key")},
    ],
)
def test_provider_rejects_missing_nonstring_and_blank_keys_without_building_service(
    monkeypatch: pytest.MonkeyPatch,
    credentials: dict[str, object],
) -> None:
    def fail_if_built(api_key: str) -> None:
        pytest.fail(f"service should not be built with {api_key!r}")

    monkeypatch.setattr(provider_module, "build_service", fail_if_built, raising=False)

    with pytest.raises(ToolProviderCredentialValidationError) as captured:
        make_provider()._validate_credentials(credentials)

    assert str(captured.value) == API_KEY_ERROR
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize(
    "error",
    [
        SerpApiError(401, "authentication rejected synthetic-key", {"key": "synthetic-key"}),
        SerpApiError(429, "rate limited synthetic-key", {"key": "synthetic-key"}),
        SchemaError("schema failure synthetic-key"),
        ResponseError("response failure synthetic-key"),
        ParameterError("parameter failure synthetic-key"),
        requests.RequestException("request failure synthetic-key"),
    ],
    ids=["auth", "rate", "schema", "response", "parameter", "request"],
)
def test_expected_service_failures_become_safe_credential_errors(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    service = RecordingService(error=error)
    monkeypatch.setattr(provider_module, "build_service", lambda api_key: service, raising=False)

    with pytest.raises(ToolProviderCredentialValidationError) as captured:
        make_provider()._validate_credentials({"api_key": "synthetic-key"})

    assert str(captured.value) == safe_error_message(error, "synthetic-key")
    assert service.calls == [EXPECTED_SEARCH]
    assert_secret_free_failure(captured.value, "synthetic-key")


def test_unexpected_value_error_uses_generic_safe_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = ValueError("internal path/config detail synthetic-key")
    service = RecordingService(error=error)
    monkeypatch.setattr(provider_module, "build_service", lambda api_key: service)

    with pytest.raises(ToolProviderCredentialValidationError) as captured:
        make_provider()._validate_credentials({"api_key": "synthetic-key"})

    assert str(captured.value) == UNEXPECTED_ERROR
    assert "internal path/config detail" not in str(captured.value)
    assert service.calls == [EXPECTED_SEARCH]
    assert_secret_free_failure(captured.value, "synthetic-key")


def test_expected_build_service_failure_becomes_safe_credential_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = SchemaError("cannot build service for synthetic-key")
    build_calls: list[str] = []

    def failing_build_service(api_key: str) -> None:
        build_calls.append(api_key)
        raise error

    monkeypatch.setattr(provider_module, "build_service", failing_build_service, raising=False)

    with pytest.raises(ToolProviderCredentialValidationError) as captured:
        make_provider()._validate_credentials({"api_key": "synthetic-key"})

    assert build_calls == ["synthetic-key"]
    assert str(captured.value) == safe_error_message(error, "synthetic-key")
    assert_secret_free_failure(captured.value, "synthetic-key")


@pytest.mark.parametrize("stage", ["build", "search"])
def test_unexpected_programmer_failures_use_generic_safe_message(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    error = BrokenStringError("internal failure synthetic-key")
    service = RecordingService(error=error)

    def fake_build_service(api_key: str) -> RecordingService:
        if stage == "build":
            raise error
        return service

    monkeypatch.setattr(provider_module, "build_service", fake_build_service, raising=False)

    with pytest.raises(ToolProviderCredentialValidationError) as captured:
        make_provider()._validate_credentials({"api_key": "synthetic-key"})

    assert str(captured.value) == UNEXPECTED_ERROR
    assert service.calls == ([] if stage == "build" else [EXPECTED_SEARCH])
    assert_secret_free_failure(captured.value, "synthetic-key")
