import sys
import traceback
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dify_plugin.errors.tool as sdk_tool_errors
import pytest
import requests
from dify_plugin import Tool
from dify_plugin.core.server.io_server import IOServer
from dify_plugin.core.utils.class_loader import get_subclasses_from_module
from dify_plugin.entities.tool import InvokeMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from thordata_serp.response import ResponseError, SerpApiError  # noqa: E402
from thordata_serp.schema import SchemaError  # noqa: E402
from thordata_serp.serializer import ParameterError  # noqa: E402
from tools import _base  # noqa: E402
from tools._base import ToolInvokeError  # noqa: E402
from tools.get_engine_schema import GetEngineSchemaTool  # noqa: E402
from tools.list_engines import ListEnginesTool  # noqa: E402
from tools.search import SearchTool  # noqa: E402

API_KEY_ERROR = "A Thordata SERP API key is required."
UNEXPECTED_TOOL_ERROR = "Unexpected Thordata SERP tool failure."


class RecordingService:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_engines(self) -> dict[str, Any]:
        self.calls.append(("list_engines", {}))
        return self.result

    def get_engine_schema(self, engine: str) -> dict[str, Any]:
        self.calls.append(("get_engine_schema", {"engine": engine}))
        return self.result

    def search(self, **parameters: Any) -> dict[str, Any]:
        self.calls.append(("search", parameters))
        return self.result


def invoke_with_service(
    monkeypatch: pytest.MonkeyPatch,
    tool_class: type[ListEnginesTool | GetEngineSchemaTool | SearchTool],
    parameters: dict[str, Any],
) -> tuple[RecordingService, list[Any], list[str], dict[str, Any]]:
    result = {"ok": True, "nested": {"items": [1, 2]}}
    original = deepcopy(result)
    service = RecordingService(result)
    built_with: list[str] = []

    def fake_build_service(api_key: str) -> RecordingService:
        built_with.append(api_key)
        return service

    monkeypatch.setattr(_base, "build_service", fake_build_service)
    tool = tool_class.from_credentials({"api_key": "  synthetic-key  "})
    messages = list(tool.invoke(tool_parameters=parameters))

    assert result == original
    return service, messages, built_with, result


def assert_result_messages(messages: list[Any], result: dict[str, Any]) -> None:
    assert len(messages) == 2
    assert messages[0].type is InvokeMessage.MessageType.JSON
    assert messages[0].message.json_object == result
    assert messages[1].type is InvokeMessage.MessageType.VARIABLE
    assert messages[1].message.variable_name == "result"
    assert messages[1].message.variable_value == result


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


def test_list_engines_calls_service_and_emits_one_json_then_result_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, messages, built_with, result = invoke_with_service(monkeypatch, ListEnginesTool, {})

    assert built_with == ["synthetic-key"]
    assert service.calls == [("list_engines", {})]
    assert_result_messages(messages, result)


def test_get_engine_schema_passes_the_engine_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, messages, built_with, result = invoke_with_service(
        monkeypatch, GetEngineSchemaTool, {"engine": "google-news"}
    )

    assert built_with == ["synthetic-key"]
    assert service.calls == [("get_engine_schema", {"engine": "google-news"})]
    assert_result_messages(messages, result)


@pytest.mark.parametrize(
    ("parameters", "expected"),
    [
        (
            {
                "engine": "google",
                "query": "weather",
                "parameters_json": '{"country":"us"}',
                "response_format": "2",
                "response_mode": "complete",
            },
            {
                "engine": "google",
                "query": "weather",
                "parameters_json": '{"country":"us"}',
                "response_format": "2",
                "response_mode": "complete",
            },
        ),
        (
            {},
            {
                "engine": "",
                "query": "",
                "parameters_json": "{}",
                "response_format": "1",
                "response_mode": "compact",
            },
        ),
    ],
)
def test_search_passes_all_five_service_keywords_with_deterministic_defaults(
    monkeypatch: pytest.MonkeyPatch,
    parameters: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    service, messages, built_with, result = invoke_with_service(monkeypatch, SearchTool, parameters)

    assert built_with == ["synthetic-key"]
    assert service.calls == [("search", expected)]
    assert_result_messages(messages, result)


@pytest.mark.parametrize("credentials", [{}, {"api_key": 123}, {"api_key": "   "}])
def test_tools_reject_missing_nonstring_and_blank_api_keys(
    monkeypatch: pytest.MonkeyPatch,
    credentials: dict[str, object],
) -> None:
    def fail_if_built(api_key: str) -> None:
        pytest.fail(f"service should not be built with {api_key!r}")

    monkeypatch.setattr(_base, "build_service", fail_if_built)
    tool = ListEnginesTool.from_credentials(credentials)

    with pytest.raises(ToolInvokeError) as captured:
        list(tool.invoke(tool_parameters={}))

    assert str(captured.value) == API_KEY_ERROR
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


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
def test_expected_service_errors_become_safe_tool_invoke_errors(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    class FailingService:
        def list_engines(self) -> dict[str, Any]:
            raise error

    monkeypatch.setattr(_base, "build_service", lambda api_key: FailingService())
    tool = ListEnginesTool.from_credentials({"api_key": "synthetic-key"})

    with pytest.raises(ToolInvokeError) as captured:
        list(tool.invoke(tool_parameters={}))

    chain = exception_graph(captured.value)
    assert chain == [captured.value]
    for item in chain:
        assert "synthetic-key" not in str(item)
        assert "synthetic-key" not in repr(item)
    assert "[REDACTED]" in str(captured.value)


@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
def test_unexpected_errors_become_generic_safe_tool_errors(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    secret = "synthetic-key"

    class BrokenService:
        def list_engines(self) -> dict[str, Any]:
            try:
                raise RuntimeError(f"nested traceback contains {secret}")
            except RuntimeError as nested:
                raise error_type(f"internal details contain {secret}") from nested

    monkeypatch.setattr(_base, "build_service", lambda api_key: BrokenService())
    tool = ListEnginesTool.from_credentials({"api_key": secret})

    with pytest.raises(ToolInvokeError) as captured:
        list(tool.invoke(tool_parameters={}))

    assert str(captured.value) == UNEXPECTED_TOOL_ERROR
    assert exception_graph(captured.value) == [captured.value]
    rendered_error = repr(captured.value) + "".join(traceback.format_exception(captured.value))
    assert secret not in rendered_error
    assert "internal details" not in rendered_error
    assert "nested traceback" not in rendered_error


def test_unexpected_error_with_broken_string_conversion_is_generic_and_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "synthetic-key"

    class BrokenStringError(Exception):
        def __str__(self) -> str:
            raise RuntimeError(f"string conversion exposed {secret}")

    error = BrokenStringError(f"exception repr exposed {secret}")

    class BrokenService:
        def list_engines(self) -> dict[str, Any]:
            raise error

    assert secret in repr(error)
    with pytest.raises(RuntimeError, match=secret):
        str(error)

    monkeypatch.setattr(_base, "build_service", lambda api_key: BrokenService())
    tool = ListEnginesTool.from_credentials({"api_key": secret})

    with pytest.raises(ToolInvokeError) as captured:
        list(tool.invoke(tool_parameters={}))

    assert str(captured.value) == UNEXPECTED_TOOL_ERROR
    assert exception_graph(captured.value) == [captured.value]
    rendered_error = repr(captured.value) + "".join(traceback.format_exception(captured.value))
    assert secret not in rendered_error


def test_tool_invoke_error_is_local_and_does_not_mutate_the_sdk_module() -> None:
    assert issubclass(ToolInvokeError, RuntimeError)
    assert not hasattr(sdk_tool_errors, "ToolInvokeError")


@pytest.mark.parametrize(
    ("module", "expected_class"),
    [
        (__import__("tools.list_engines", fromlist=["ListEnginesTool"]), ListEnginesTool),
        (
            __import__("tools.get_engine_schema", fromlist=["GetEngineSchemaTool"]),
            GetEngineSchemaTool,
        ),
        (__import__("tools.search", fromlist=["SearchTool"]), SearchTool),
    ],
)
def test_dify_sdk_loader_finds_exactly_one_tool_subclass(
    module: Any,
    expected_class: type[Tool],
) -> None:
    assert get_subclasses_from_module(module, Tool) == [expected_class]


def test_sdk_runner_error_payload_does_not_leak_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "synthetic-key"

    class FailingService:
        def list_engines(self) -> dict[str, Any]:
            try:
                raise RuntimeError(f"nested traceback contains {secret}")
            except RuntimeError as nested:
                raise ValueError(f"internal details contain {secret}") from nested

    class InvokeServer(IOServer):
        def __init__(self, tool: ListEnginesTool) -> None:
            self.tool = tool
            super().__init__(SimpleNamespace(MAX_WORKER=1), object(), None)

        def _execute_request(self, session_id: str, data: dict, *args: Any, **kwargs: Any) -> None:
            list(self.tool.invoke(tool_parameters=data))

    class RecordingWriter:
        def __init__(self) -> None:
            self.messages: list[dict[str, Any]] = []
            self.done_called = False

        def stream_error_object(self, data: dict[str, Any]) -> dict[str, Any]:
            return data

        def stream_end_object(self) -> dict[str, str]:
            return {"event": "end"}

        def session_message(self, session_id: str, data: dict[str, Any]) -> None:
            self.messages.append(data)

        def done(self) -> None:
            self.done_called = True

    monkeypatch.setattr(_base, "build_service", lambda api_key: FailingService())
    tool = ListEnginesTool.from_credentials({"api_key": secret})
    server = InvokeServer(tool)
    writer = RecordingWriter()
    try:
        server._execute_request_in_thread("session", {}, object(), writer)
    finally:
        server.executer.shutdown(wait=True)

    error_payload = writer.messages[0]
    assert error_payload["error_type"] == "ToolInvokeError"
    assert error_payload["message"] == UNEXPECTED_TOOL_ERROR
    assert "Traceback" in error_payload["args"]["traceback"]
    assert secret not in repr(error_payload)
    assert "internal details" not in repr(error_payload)
    assert "nested traceback" not in repr(error_payload)
    assert writer.messages[1] == {"event": "end"}
    assert writer.done_called is True
