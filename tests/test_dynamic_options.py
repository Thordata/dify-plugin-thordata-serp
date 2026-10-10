from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import requests
from dify_plugin.entities import I18nObject, ParameterOption

from thordata_serp.response import ResponseError, SerpApiError
from thordata_serp.schema import SchemaError, SchemaRepository
from thordata_serp.serializer import ParameterError
from thordata_serp.service import ThordataSerpService
from tools import _base
from tools._base import ToolInvokeError
from tools.generated.bing import BingTool
from tools.generated.google_patents import GooglePatentsTool
from tools.generated.google_web import GoogleWebTool

ROOT = Path(__file__).resolve().parents[1]


class _SnapshotResource:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def read_text(self, *, encoding: str) -> str:
        assert encoding == "utf-8"
        return json.dumps(self.payload)


class _RemoteSchema:
    def __init__(self, payloads: list[dict[str, Any] | Exception]) -> None:
        self.payloads = payloads

    def fetch(self) -> dict[str, Any]:
        payload = self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return payload


class _SerpClient:
    api_key = "synthetic-key"

    def request(self, form: dict[str, Any]) -> tuple[int, bytes]:
        return 200, b'{"data":{"organic":[]}}'


def _schema_payload() -> dict[str, Any]:
    return json.loads(
        (ROOT / "thordata_serp" / "serp-schema.snapshot.json").read_text(encoding="utf-8")
    )


def test_generated_invoke_uses_fixed_engine_and_excludes_user_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    class Service:
        def search_fixed_engine(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"ok": True, "data": {}}

    monkeypatch.setattr(_base, "build_service", lambda api_key: Service())
    tool = GooglePatentsTool.from_credentials({"api_key": "synthetic-key"})

    list(
        tool.invoke(
            tool_parameters={
                "q": "coffee",
                "engine": "bing",
                "num": 3,
                "extra_parameters_json": '{"json":"2"}',
                "response_format": "2",
                "response_mode": "complete",
            }
        )
    )

    assert calls == [
        {
            "engine": "google_patents",
            "query": "coffee",
            "parameters": {"num": 3},
            "extra_parameters_json": '{"json":"2"}',
            "response_format": "2",
            "response_mode": "complete",
        }
    ]


def test_dynamic_options_use_parameter_models_and_refresh_then_snapshot_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _schema_payload()

    class Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    clock = Clock()
    repository = SchemaRepository(
        _RemoteSchema([payload, RuntimeError("remote unavailable")]),
        _SnapshotResource(payload),
        ttl=0.1,
        clock=clock,
    )
    service = ThordataSerpService(
        "synthetic-key",
        schema_repository=repository,
        serp_client=_SerpClient(),
    )
    monkeypatch.setattr(_base, "build_service", lambda api_key: service)
    tool = GoogleWebTool.from_credentials({"api_key": "synthetic-key"})

    first = tool._fetch_parameter_options("device")
    assert first == [
        ParameterOption(value="desktop", label=I18nObject(en_US="Desktop")),
        ParameterOption(value="tablet", label=I18nObject(en_US="Tablet")),
        ParameterOption(value="mobile", label=I18nObject(en_US="Mobile")),
    ]

    clock.value = 1.0
    second = tool._fetch_parameter_options("device")
    assert second == first


def test_google_location_dynamic_options_are_plain_schema_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _schema_payload()
    service = ThordataSerpService(
        "synthetic-key",
        schema_repository=SchemaRepository(
            _RemoteSchema([payload]),
            _SnapshotResource(payload),
        ),
        serp_client=_SerpClient(),
    )
    monkeypatch.setattr(_base, "build_service", lambda api_key: service)
    tool = GoogleWebTool.from_credentials({"api_key": "synthetic-key"})

    options = tool._fetch_parameter_options("location")
    assert options
    assert all("UULE:" not in option.label.en_us for option in options)
    assert next(option for option in options if option.value).label.en_us == "India"


def test_bing_location_dynamic_options_do_not_include_uule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _schema_payload()
    service = ThordataSerpService(
        "synthetic-key",
        schema_repository=SchemaRepository(
            _RemoteSchema([payload]),
            _SnapshotResource(payload),
        ),
        serp_client=_SerpClient(),
    )
    monkeypatch.setattr(_base, "build_service", lambda api_key: service)
    tool = BingTool.from_credentials({"api_key": "synthetic-key"})

    options = tool._fetch_parameter_options("location")
    assert options
    assert all("UULE:" not in option.label.en_us for option in options)


def test_dynamic_options_unknown_or_hidden_field_is_empty_and_secret_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _schema_payload()
    service = ThordataSerpService(
        "synthetic-key",
        schema_repository=SchemaRepository(
            _RemoteSchema([payload]),
            _SnapshotResource(payload),
        ),
        serp_client=_SerpClient(),
    )
    monkeypatch.setattr(_base, "build_service", lambda api_key: service)
    tool = GoogleWebTool.from_credentials({"api_key": "synthetic-key"})

    assert tool._fetch_parameter_options("does-not-exist") == []
    assert tool._fetch_parameter_options("uule") == []


def test_array_parameters_reach_service_as_lists_and_location_generates_uule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    class Service:
        def search_fixed_engine(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"ok": True, "data": {}}

    monkeypatch.setattr(_base, "build_service", lambda api_key: Service())
    tool = GoogleWebTool.from_credentials({"api_key": "synthetic-key"})
    list(
        tool.invoke(
            tool_parameters={
                "q": "coffee",
                "cr": ["us"],
                "lr": ["lang_en"],
                "location": "New York",
            }
        )
    )

    assert calls[0]["parameters"]["cr"] == ["us"]
    assert calls[0]["parameters"]["lr"] == ["lang_en"]
    assert calls[0]["parameters"]["location"] == "New York"

    forms: list[dict[str, Any]] = []

    class RecordingClient(_SerpClient):
        def request(self, form: dict[str, Any]) -> tuple[int, bytes]:
            forms.append(dict(form))
            return super().request(form)

    service = ThordataSerpService(
        "synthetic-key",
        schema_repository=SchemaRepository(
            _RemoteSchema([_schema_payload()]),
            _SnapshotResource(_schema_payload()),
        ),
        serp_client=RecordingClient(),
    )
    monkeypatch.setattr(_base, "build_service", lambda api_key: service)
    list(tool.invoke(tool_parameters={"q": "coffee", "location": "New York"}))
    assert forms[0]["uule"].startswith("w+CAIQICI")


@pytest.mark.parametrize(
    "error",
    [
        SchemaError("schema failure synthetic-key"),
        ParameterError("parameter failure synthetic-key"),
        ResponseError("response failure synthetic-key"),
        SerpApiError(429, "API failure synthetic-key", {"secret": "synthetic-key"}),
        requests.RequestException("request failure synthetic-key"),
    ],
)
def test_dynamic_option_known_errors_are_safe_tool_errors(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    class FailingService:
        def get_engine_schema(self, engine: str) -> dict[str, Any]:
            raise error

    monkeypatch.setattr(_base, "build_service", lambda api_key: FailingService())
    tool = GoogleWebTool.from_credentials({"api_key": "synthetic-key"})

    with pytest.raises(ToolInvokeError) as captured:
        tool._fetch_parameter_options("device")

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "synthetic-key" not in repr(captured.value)
    assert "[REDACTED]" in str(captured.value)


@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
def test_dynamic_option_unknown_errors_are_generic_and_secret_free(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    secret = "synthetic-key"

    class BrokenService:
        def get_engine_schema(self, engine: str) -> dict[str, Any]:
            raise error_type(f"internal details contain {secret}")

    monkeypatch.setattr(_base, "build_service", lambda api_key: BrokenService())
    tool = GoogleWebTool.from_credentials({"api_key": secret})

    with pytest.raises(ToolInvokeError) as captured:
        tool._fetch_parameter_options("device")

    assert str(captured.value) == "Unexpected Thordata SERP tool failure."
    assert secret not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_dynamic_option_broken_error_string_is_generic_and_secret_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "synthetic-key"

    class BrokenStringError(Exception):
        def __str__(self) -> str:
            raise RuntimeError(f"string conversion exposed {secret}")

    error = BrokenStringError(f"exception repr exposed {secret}")

    class BrokenService:
        def get_engine_schema(self, engine: str) -> dict[str, Any]:
            raise error

    monkeypatch.setattr(_base, "build_service", lambda api_key: BrokenService())
    tool = GoogleWebTool.from_credentials({"api_key": secret})

    with pytest.raises(ToolInvokeError) as captured:
        tool._fetch_parameter_options("device")

    assert str(captured.value) == "Unexpected Thordata SERP tool failure."
    assert secret not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
