import json
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import thordata_serp.service as service_module  # noqa: E402
from thordata_serp.client import (  # noqa: E402
    MAX_REQUEST_SIZE,
    RequestEncodingError,
    SerpClient,
)
from thordata_serp.response import ResponseError, SerpApiError  # noqa: E402
from thordata_serp.schema import SchemaError, validate_schema_payload  # noqa: E402
from thordata_serp.serializer import ParameterError  # noqa: E402
from thordata_serp.service import ThordataSerpService, build_service  # noqa: E402


class StubRepository:
    def __init__(self, schema, source="remote", error=None):
        self.schema = schema
        self.source = source
        self.error = error
        self.calls = 0

    def current(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.schema, self.source


class StubSerpClient:
    def __init__(self, response=None, error=None, api_key="secret"):
        self.response = response or (200, b'{"data":{"organic":[{"title":"Coffee"}]}}')
        self.error = error
        self.api_key = api_key
        self.forms = []

    def request(self, form):
        self.forms.append(deepcopy(form))
        if self.error is not None:
            raise self.error
        return self.response


@pytest.fixture
def schema(schema_payload):
    schema_payload["data"]["categories"][0]["engines"][0]["groups"][0]["fields"][0]["required"] = (
        False
    )
    return validate_schema_payload(schema_payload)


@pytest.fixture
def service(schema):
    return ThordataSerpService(
        " synthetic-secret ",
        schema_repository=StubRepository(schema),
        serp_client=StubSerpClient(api_key="synthetic-secret"),
    )


def _exception_graph(error):
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        cause = getattr(current, "__cause__", None)
        context = getattr(current, "__context__", None)
        if cause is not None:
            pending.append(cause)
        if context is not None:
            pending.append(context)


def test_list_engines_returns_exact_ordered_defensive_envelope(schema) -> None:
    second = deepcopy(schema["categories"][0]["engines"][0])
    second.update({"key": "bing", "name": "Bing", "query_field": "query"})
    schema["engines"] = [second]
    repository = StubRepository(schema, source="snapshot")
    service = ThordataSerpService(
        "secret", schema_repository=repository, serp_client=StubSerpClient()
    )

    result = service.list_engines()

    assert result == {
        "ok": True,
        "schema_version": "1",
        "audience": "is_serp_old=0",
        "default_engine": "google",
        "schema_source": "snapshot",
        "engines": [
            {"key": "google", "name": "Search", "query_field": "q"},
            {"key": "bing", "name": "Bing", "query_field": "query"},
        ],
    }
    result["engines"][0]["name"] = "Changed"
    assert schema["categories"][0]["engines"][0]["name"] == "Search"


def test_list_engines_redacts_schema_values_without_mutating_repository(schema) -> None:
    secret = "synthetic-secret"
    engine = schema["categories"][0]["engines"][0]
    schema["schema_version"] = f"version-{secret}"
    engine["name"] = f"name-{secret}"
    engine["groups"][0][f"private-{secret}"] = {"value": secret}
    repository = StubRepository(schema, source=f"source-{secret}")
    original = deepcopy(schema)
    service = ThordataSerpService(
        secret,
        schema_repository=repository,
        serp_client=StubSerpClient(api_key=secret),
    )

    result = service.list_engines()

    assert secret not in repr(result)
    assert "[REDACTED]" in repr(result)
    assert repository.schema == original


def test_get_engine_schema_returns_complete_copy_and_trimmed_lookup(schema) -> None:
    repository = StubRepository(schema, source="cache")
    service = ThordataSerpService(
        "secret", schema_repository=repository, serp_client=StubSerpClient()
    )

    result = service.get_engine_schema(" google ")

    assert result == {
        "ok": True,
        "schema_source": "cache",
        "engine": schema["categories"][0]["engines"][0],
    }
    result["engine"]["groups"][0]["fields"][0]["key"] = "changed"
    assert schema["categories"][0]["engines"][0]["groups"][0]["fields"][0]["key"] == "q"


def test_get_engine_schema_redacts_nested_keys_and_values_without_mutating_repository(
    schema,
) -> None:
    secret = "synthetic-secret"
    engine = schema["categories"][0]["engines"][0]
    engine["groups"][0][f"private-{secret}"] = {f"nested-{secret}": [f"value-{secret}"]}
    repository = StubRepository(schema, source=f"source-{secret}")
    original = deepcopy(schema)
    service = ThordataSerpService(
        secret,
        schema_repository=repository,
        serp_client=StubSerpClient(api_key=secret),
    )

    result = service.get_engine_schema("google")

    assert secret not in repr(result)
    assert "[REDACTED]" in repr(result)
    assert repository.schema == original


@pytest.mark.parametrize("engine", [None, 7, "", "   "])
def test_get_engine_schema_rejects_nonblank_nonstring_engine(schema, engine) -> None:
    service = ThordataSerpService(
        "secret", schema_repository=StubRepository(schema), serp_client=StubSerpClient()
    )

    with pytest.raises(SchemaError, match="nonblank string"):
        service.get_engine_schema(engine)


def test_get_engine_schema_does_not_fall_back_for_unknown_engine(service) -> None:
    with pytest.raises(SchemaError, match="unknown engine: missing"):
        service.get_engine_schema("missing")


def test_search_returns_stable_success_envelope_and_exact_form(schema) -> None:
    client = StubSerpClient()
    service = ThordataSerpService(
        "secret", schema_repository=StubRepository(schema), serp_client=client
    )

    result = service.search(
        engine="google",
        query="coffee",
        parameters_json='{"q":"tea","num":5}',
        response_format="1",
        response_mode="compact",
    )

    assert result == {
        "ok": True,
        "status": 200,
        "engine": "google",
        "data": {"organic": [{"title": "Coffee"}]},
    }
    assert client.forms == [{"q": "coffee", "num": "5", "json": "1", "engine": "google"}]


def test_search_uses_schema_default_engine(schema) -> None:
    client = StubSerpClient()
    service = ThordataSerpService(
        "secret", schema_repository=StubRepository(schema), serp_client=client
    )

    result = service.search(query="coffee")

    assert result["engine"] == "google"
    assert client.forms == [{"q": "coffee", "json": "1", "engine": "google"}]


def test_search_fixed_engine_has_exact_signature_and_rejects_engine_override(schema) -> None:
    engine = deepcopy(schema["categories"][0]["engines"][0])
    engine["key"] = "google_web"
    fields = engine["groups"][0]["fields"]
    fields.extend(
        [
            {
                "key": "location",
                "label": "Location",
                "type": "string",
                "control": "input",
                "visible": True,
                "required": False,
            },
            {
                "key": "uule",
                "label": "UULE",
                "type": "string",
                "control": "input",
                "visible": True,
                "required": False,
            },
        ]
    )
    schema["categories"][0]["engines"].append(engine)
    client = StubSerpClient()
    service = ThordataSerpService(
        "secret", schema_repository=StubRepository(schema), serp_client=client
    )

    result = service.search_fixed_engine(
        engine="google_web",
        query="coffee",
        parameters={"engine": "google", "location": "United States"},
        extra_parameters_json='{"engine":"google"}',
    )

    assert result["engine"] == "google_web"
    assert client.forms[0]["engine"] == "google_web"
    assert client.forms[0]["q"] == "coffee"
    assert client.forms[0]["location"] == "United States"
    assert client.forms[0]["uule"].startswith("w+CAIQICI")


def test_search_fixed_engine_preserves_explicit_uule_and_does_not_derive_for_hidden_fields(
    schema,
):
    visible = deepcopy(schema["categories"][0]["engines"][0])
    visible["key"] = "google_web"
    visible["groups"][0]["fields"].extend(
        [
            {
                "key": "location",
                "label": "Location",
                "type": "string",
                "control": "input",
                "visible": True,
                "required": False,
            },
            {
                "key": "uule",
                "label": "UULE",
                "type": "string",
                "control": "input",
                "visible": True,
                "required": False,
            },
        ]
    )
    schema["categories"][0]["engines"].append(visible)
    hidden = deepcopy(schema["categories"][0]["engines"][0])
    hidden["key"] = "hidden_location"
    hidden["groups"][0]["fields"].extend(
        [
            {
                "key": "location",
                "label": "Location",
                "type": "string",
                "control": "input",
                "visible": False,
                "required": False,
            },
            {
                "key": "uule",
                "label": "UULE",
                "type": "string",
                "control": "input",
                "visible": False,
                "required": False,
            },
        ]
    )
    schema["categories"][0]["engines"].append(hidden)
    client = StubSerpClient()
    service = ThordataSerpService(
        "secret", schema_repository=StubRepository(schema), serp_client=client
    )

    service.search_fixed_engine(
        engine="google_web",
        parameters={"location": "United States", "uule": "explicit"},
    )
    assert client.forms[0]["uule"] == "explicit"

    service.search_fixed_engine(engine="hidden_location", parameters={"location": "United States"})
    assert "uule" not in client.forms[1]


def test_search_fixed_engine_derives_uule_from_visible_location_default(schema):
    engine = deepcopy(schema["categories"][0]["engines"][0])
    engine["key"] = "default_location"
    engine["groups"][0]["fields"].extend(
        [
            {
                "key": "location",
                "label": "Location",
                "type": "string",
                "control": "input",
                "visible": True,
                "required": False,
                "default_value": "United States",
            },
            {
                "key": "uule",
                "label": "UULE",
                "type": "string",
                "control": "input",
                "visible": True,
                "required": False,
            },
        ]
    )
    schema["categories"][0]["engines"].append(engine)
    client = StubSerpClient()
    service = ThordataSerpService(
        "secret", schema_repository=StubRepository(schema), serp_client=client
    )

    service.search_fixed_engine(engine="default_location")

    assert client.forms[0]["location"] == "United States"
    assert client.forms[0]["uule"].startswith("w+CAIQICI")


def test_search_fixed_engine_extra_query_overrides_mapped_query(schema):
    engine = deepcopy(schema["categories"][0]["engines"][0])
    engine["key"] = "fixed_query"
    schema["categories"][0]["engines"].append(engine)
    client = StubSerpClient()
    service = ThordataSerpService(
        "secret", schema_repository=StubRepository(schema), serp_client=client
    )

    service.search_fixed_engine(
        engine="fixed_query",
        query="mapped",
        parameters={"q": "parameter"},
        extra_parameters_json='{"q":"advanced"}',
    )

    assert client.forms[0]["q"] == "advanced"


def test_search_fixed_engine_extra_json_overrides_response_format(schema):
    engine = deepcopy(schema["categories"][0]["engines"][0])
    engine["key"] = "fixed_json"
    schema["categories"][0]["engines"].append(engine)
    client = StubSerpClient()
    service = ThordataSerpService(
        "secret", schema_repository=StubRepository(schema), serp_client=client
    )

    service.search_fixed_engine(
        engine="fixed_json",
        query="coffee",
        extra_parameters_json='{"json":"0"}',
        response_format="1",
    )

    assert client.forms[0]["json"] == "0"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(" 3 ", "3"), (1.0, "1")],
)
def test_search_fixed_engine_normalizes_scalar_extra_json(schema, value, expected):
    engine = deepcopy(schema["categories"][0]["engines"][0])
    engine["key"] = "fixed_json_scalar"
    schema["categories"][0]["engines"].append(engine)
    client = StubSerpClient()
    service = ThordataSerpService(
        "secret", schema_repository=StubRepository(schema), serp_client=client
    )

    service.search_fixed_engine(
        engine="fixed_json_scalar",
        query="coffee",
        extra_parameters_json=json.dumps({"json": value}),
    )

    assert client.forms[0]["json"] == expected


@pytest.mark.parametrize("value", [{"nested": True}, ["3"]])
def test_search_fixed_engine_rejects_non_scalar_extra_json(schema, value):
    engine = deepcopy(schema["categories"][0]["engines"][0])
    engine["key"] = "fixed_json_type"
    schema["categories"][0]["engines"].append(engine)
    service = ThordataSerpService(
        "secret", schema_repository=StubRepository(schema), serp_client=StubSerpClient()
    )

    with pytest.raises(ParameterError, match="json.*scalar"):
        service.search_fixed_engine(
            engine="fixed_json_type",
            query="coffee",
            extra_parameters_json=json.dumps({"json": value}),
        )


def test_search_fixed_engine_uule_requires_effectively_visible_location_and_uule(schema):
    engine = deepcopy(schema["categories"][0]["engines"][0])
    engine["key"] = "conditional_location"
    engine["groups"][0]["fields"].extend(
        [
            {
                "key": "mode",
                "label": "Mode",
                "type": "options",
                "control": "select",
                "visible": True,
                "required": False,
                "options": [
                    {"label": "Basic", "value": "basic"},
                    {"label": "Advanced", "value": "advanced"},
                ],
            },
            {
                "key": "location",
                "label": "Location",
                "type": "string",
                "control": "input",
                "visible": True,
                "required": False,
                "show_when": {"field": "mode", "operator": "equals", "values": ["advanced"]},
            },
            {
                "key": "uule",
                "label": "UULE",
                "type": "string",
                "control": "input",
                "visible": True,
                "required": False,
                "show_when": {"field": "mode", "operator": "equals", "values": ["advanced"]},
            },
        ]
    )
    schema["categories"][0]["engines"].append(engine)
    client = StubSerpClient()
    service = ThordataSerpService(
        "secret", schema_repository=StubRepository(schema), serp_client=client
    )

    service.search_fixed_engine(
        engine="conditional_location",
        parameters={"mode": "basic", "location": "United States"},
    )
    service.search_fixed_engine(
        engine="conditional_location",
        parameters={"mode": " advanced ", "location": "United States"},
    )

    assert "uule" not in client.forms[0]
    assert client.forms[1]["uule"].startswith("w+CAIQICI")


def test_search_complete_preserves_metadata_while_compact_removes_it(schema) -> None:
    payload = {
        "data": {
            "search_metadata": {"id": "one"},
            "organic": [{"title": "Coffee", "request_metadata": {"id": "two"}}],
        }
    }
    client = StubSerpClient(response=(201, json.dumps(payload).encode()))
    service = ThordataSerpService(
        "secret", schema_repository=StubRepository(schema), serp_client=client
    )

    complete = service.search(query="coffee", response_mode="complete")
    compact = service.search(query="coffee", response_mode="compact")

    assert complete == {
        "ok": True,
        "status": 201,
        "engine": "google",
        "data": payload["data"],
    }
    assert compact == {
        "ok": True,
        "status": 201,
        "engine": "google",
        "data": {"organic": [{"title": "Coffee"}]},
    }


@pytest.mark.parametrize("mode", ["", "Compact", " complete ", None, 1, [], {}])
def test_search_rejects_invalid_response_mode_without_dependencies(schema, mode) -> None:
    repository = StubRepository(schema)
    client = StubSerpClient()
    service = ThordataSerpService("secret", schema_repository=repository, serp_client=client)

    with pytest.raises(ParameterError, match="compact.*complete"):
        service.search(response_mode=mode)

    assert repository.calls == 0
    assert client.forms == []


@pytest.mark.parametrize("parameters", ["{", "[]", 7])
def test_search_propagates_malformed_parameter_errors(service, parameters) -> None:
    with pytest.raises(ParameterError):
        service.search(parameters_json=parameters)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"response_format": "4"}, "response_format"),
        ({"query": 7}, "query"),
        ({"engine": 7}, "engine_key"),
    ],
)
def test_search_propagates_serializer_errors(service, arguments, message) -> None:
    with pytest.raises(ParameterError, match=message):
        service.search(**arguments)


@pytest.mark.parametrize(
    ("response", "error_type"),
    [
        ((401, b'{"message":"denied"}'), SerpApiError),
        ((200, b'{"code":429,"message":"limited"}'), SerpApiError),
        ((200, b"not-json"), ResponseError),
    ],
)
def test_search_preserves_typed_response_errors(schema, response, error_type) -> None:
    service = ThordataSerpService(
        "secret",
        schema_repository=StubRepository(schema),
        serp_client=StubSerpClient(response=response),
    )

    with pytest.raises(error_type):
        service.search()


def test_service_preserves_schema_and_parameter_errors(schema) -> None:
    schema_error = SchemaError("schema unavailable")
    service = ThordataSerpService(
        "secret",
        schema_repository=StubRepository(schema, error=schema_error),
        serp_client=StubSerpClient(),
    )

    with pytest.raises(SchemaError, match="schema unavailable"):
        service.list_engines()

    with pytest.raises(ParameterError):
        ThordataSerpService(
            "secret", schema_repository=StubRepository(schema), serp_client=StubSerpClient()
        ).search(parameters_json="bad")


@pytest.mark.parametrize(
    ("error", "error_type"),
    [
        (ResponseError("failed with synthetic-secret"), ResponseError),
        (
            SerpApiError(
                429,
                "failed with synthetic-secret",
                {"synthetic-secret": ["synthetic-secret"]},
            ),
            SerpApiError,
        ),
    ],
)
def test_injected_typed_client_errors_remain_typed_and_are_fully_redacted(
    schema, error, error_type
) -> None:
    secret = "synthetic-secret"
    service = ThordataSerpService(
        secret,
        schema_repository=StubRepository(schema),
        serp_client=StubSerpClient(error=error, api_key=secret),
    )

    with pytest.raises(error_type) as captured:
        service.search()

    assert "[REDACTED]" in repr(captured.value)
    if isinstance(captured.value, SerpApiError):
        assert secret not in repr(captured.value.response_data)
    for chained in _exception_graph(captured.value):
        assert secret not in str(chained)
        assert secret not in repr(chained)


def test_schema_error_with_secret_remains_typed_and_has_safe_chain(schema) -> None:
    secret = "synthetic-secret"
    service = ThordataSerpService(
        secret,
        schema_repository=StubRepository(
            schema,
            error=SchemaError(f"schema failure {secret}"),
        ),
        serp_client=StubSerpClient(api_key=secret),
    )

    with pytest.raises(SchemaError) as captured:
        service.list_engines()

    assert "[REDACTED]" in str(captured.value)
    for chained in _exception_graph(captured.value):
        assert secret not in str(chained)
        assert secret not in repr(chained)


def test_serializer_error_with_secret_remains_typed_and_has_safe_chain(schema) -> None:
    secret = "synthetic-secret"
    schema["categories"][0]["engines"][0]["groups"][0]["fields"].append(
        {"key": secret, "type": "string", "required": False, "visible": True}
    )
    service = ThordataSerpService(
        secret,
        schema_repository=StubRepository(schema),
        serp_client=StubSerpClient(api_key=secret),
    )

    with pytest.raises(ParameterError) as captured:
        service.search(parameters_json=json.dumps({secret: {"invalid": True}}))

    assert "[REDACTED]" in str(captured.value)
    for chained in _exception_graph(captured.value):
        assert secret not in str(chained)
        assert secret not in repr(chained)


@pytest.mark.parametrize("operation", ["list", "get", "search"])
@pytest.mark.parametrize("error_type", [requests.Timeout, requests.ConnectionError])
def test_schema_request_exceptions_are_normalized_with_safe_chains(
    schema, operation, error_type
) -> None:
    secret = "synthetic-secret"
    repository = StubRepository(schema, error=error_type(f"network exposed {secret}"))
    service = ThordataSerpService(
        secret,
        schema_repository=repository,
        serp_client=StubSerpClient(api_key=secret),
    )

    with pytest.raises(ResponseError, match="schema request failed") as captured:
        if operation == "list":
            service.list_engines()
        elif operation == "get":
            service.get_engine_schema("google")
        else:
            service.search()

    for error in _exception_graph(captured.value):
        assert secret not in str(error)
        assert secret not in repr(error)


@pytest.mark.parametrize("error_type", [requests.Timeout, requests.ConnectionError])
def test_serp_request_exceptions_are_normalized_with_safe_chains(schema, error_type) -> None:
    secret = "synthetic-secret"
    client = StubSerpClient(
        error=error_type(f"network exposed {secret}"),
        api_key=secret,
    )
    service = ThordataSerpService(
        secret, schema_repository=StubRepository(schema), serp_client=client
    )

    with pytest.raises(ResponseError, match="SERP request failed") as captured:
        service.search()

    for error in _exception_graph(captured.value):
        assert secret not in str(error)
        assert secret not in repr(error)


def test_request_encoding_error_becomes_safe_parameter_error(schema) -> None:
    secret = "synthetic-secret"
    client = StubSerpClient(
        error=RequestEncodingError(f"SERP request exceeds maximum size for {secret}"),
        api_key=secret,
    )
    service = ThordataSerpService(
        secret,
        schema_repository=StubRepository(schema),
        serp_client=client,
    )

    with pytest.raises(ParameterError, match="exceeds maximum size") as captured:
        service.search()

    assert type(captured.value) is ParameterError
    assert "[REDACTED]" in str(captured.value)
    for error in _exception_graph(captured.value):
        assert secret not in str(error)
        assert secret not in repr(error)


def test_oversized_query_becomes_parameter_error_before_network(schema) -> None:
    secret = "synthetic-secret"

    class NoNetworkSession:
        def post(self, *args, **kwargs):
            pytest.fail("request encoding must fail before network access")

    client = SerpClient(
        secret,
        session=NoNetworkSession(),
        _endpoint="https://serp.example.test/request",
    )
    service = ThordataSerpService(
        secret,
        schema_repository=StubRepository(schema),
        serp_client=client,
    )

    with pytest.raises(ParameterError, match="exceeds maximum size") as captured:
        service.search(query="x" * MAX_REQUEST_SIZE)

    assert type(captured.value) is ParameterError
    for error in _exception_graph(captured.value):
        assert secret not in str(error)
        assert secret not in repr(error)


def test_unexpected_injected_client_value_error_propagates(schema) -> None:
    error = ValueError("programmer bug")
    service = ThordataSerpService(
        "synthetic-secret",
        schema_repository=StubRepository(schema),
        serp_client=StubSerpClient(error=error, api_key="synthetic-secret"),
    )

    with pytest.raises(ValueError, match="^programmer bug$") as captured:
        service.search()

    assert captured.value is error


@pytest.mark.parametrize("response_mode", ["compact", "complete"])
def test_search_recursively_redacts_success_without_mutating_source(schema, response_mode) -> None:
    secret = "synthetic-secret"
    source = {
        "data": {
            f"key-{secret}": f"value-{secret}",
            "items": [{"nested": secret}],
            "search_metadata": {"token": secret},
        }
    }
    original = deepcopy(source)
    client = StubSerpClient(
        response=(200, json.dumps(source).encode()),
        api_key=secret,
    )
    service = ThordataSerpService(
        secret, schema_repository=StubRepository(schema), serp_client=client
    )

    result = service.search(response_mode=response_mode)

    assert secret not in repr(result)
    assert "[REDACTED]" in repr(result)
    assert source == original


@pytest.mark.parametrize("api_key", ["", "   ", None, 7])
def test_constructor_rejects_invalid_keys_even_with_injected_dependencies(schema, api_key) -> None:
    with pytest.raises(ValueError, match="API key"):
        ThordataSerpService(
            api_key,
            schema_repository=StubRepository(schema),
            serp_client=StubSerpClient(api_key="secret"),
        )


@pytest.mark.parametrize(
    ("client", "client_secret"),
    [
        (object(), ""),
        (StubSerpClient(api_key=None), ""),
        (StubSerpClient(api_key=7), ""),
        (StubSerpClient(api_key="client-secret"), "client-secret"),
    ],
)
def test_constructor_rejects_missing_invalid_or_mismatched_injected_client_key(
    schema, client, client_secret
) -> None:
    supplied_secret = "supplied-secret"

    with pytest.raises(ValueError, match="injected SERP client API key") as captured:
        ThordataSerpService(
            supplied_secret,
            schema_repository=StubRepository(schema),
            serp_client=client,
        )

    for error in _exception_graph(captured.value):
        assert supplied_secret not in str(error)
        assert supplied_secret not in repr(error)
        if client_secret:
            assert client_secret not in str(error)
            assert client_secret not in repr(error)


def test_injected_client_exception_redacts_its_normalized_key(schema) -> None:
    normalized_key = "client-secret"
    client = StubSerpClient(
        api_key=normalized_key,
        error=requests.ConnectionError(f"request exposed {normalized_key}"),
    )
    service = ThordataSerpService(
        f"  {normalized_key}  ",
        schema_repository=StubRepository(schema),
        serp_client=client,
    )

    with pytest.raises(ResponseError) as captured:
        service.search()

    assert "[REDACTED]" in str(captured.value)
    for error in _exception_graph(captured.value):
        assert normalized_key not in str(error)
        assert normalized_key not in repr(error)


def test_constructor_uses_serp_client_validation_and_normalization(schema) -> None:
    service = ThordataSerpService("  normalized-key  ", schema_repository=StubRepository(schema))

    assert service._serp_client.api_key == "normalized-key"
    assert "normalized-key" not in repr(service)


def test_build_service_shares_repository_but_not_clients_or_keys(monkeypatch) -> None:
    repository = object()
    clients = []

    class RecordingClient:
        def __init__(self, api_key):
            self.api_key = api_key.strip()
            clients.append(self)

    monkeypatch.setattr(service_module, "_shared_schema_repository", repository)
    monkeypatch.setattr(service_module, "SerpClient", RecordingClient)

    first = build_service(" first-key ")
    second = build_service(" second-key ")

    assert first._schema_repository is repository
    assert second._schema_repository is repository
    assert first._serp_client is clients[0]
    assert second._serp_client is clients[1]
    assert first._serp_client is not second._serp_client
    assert (clients[0].api_key, clients[1].api_key) == ("first-key", "second-key")
    assert "first-key" not in repr(first)
    assert "second-key" not in repr(second)


def test_concurrent_factory_initializes_one_repository_with_packaged_snapshot(monkeypatch) -> None:
    schema_clients = []
    repositories = []
    resource_packages = []
    joined_names = []

    class RecordingSchemaClient:
        def __init__(self):
            schema_clients.append(self)

    class RecordingRepository:
        def __init__(self, client, snapshot_path, ttl):
            self.client = client
            self.snapshot_path = snapshot_path
            self.ttl = ttl
            repositories.append(self)

    class ResourceRoot:
        def joinpath(self, name):
            joined_names.append(name)
            return ("packaged-resource", name)

    def files(package):
        resource_packages.append(package)
        return ResourceRoot()

    class RecordingSerpClient:
        def __init__(self, api_key):
            self.api_key = api_key.strip()

    monkeypatch.setattr(service_module, "_shared_schema_repository", None)
    monkeypatch.setattr(service_module, "SchemaHttpClient", RecordingSchemaClient)
    monkeypatch.setattr(service_module, "SchemaRepository", RecordingRepository)
    monkeypatch.setattr(service_module.resources, "files", files)
    monkeypatch.setattr(service_module, "SerpClient", RecordingSerpClient)

    with ThreadPoolExecutor(max_workers=16) as executor:
        services = list(executor.map(lambda index: build_service(f"key-{index}"), range(64)))

    assert len(schema_clients) == 1
    assert len(repositories) == 1
    assert {id(item._schema_repository) for item in services} == {id(repositories[0])}
    assert repositories[0].ttl == 300
    assert repositories[0].snapshot_path == (
        "packaged-resource",
        "serp-schema.snapshot.json",
    )
    assert resource_packages == ["thordata_serp"]
    assert joined_names == ["serp-schema.snapshot.json"]


def test_service_module_has_no_dify_plugin_dependency() -> None:
    source = Path(service_module.__file__).read_text(encoding="utf-8")

    assert "dify_plugin" not in source
