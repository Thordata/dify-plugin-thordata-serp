"""Framework-neutral service boundary for Thordata SERP operations."""

import math
import threading
from collections.abc import Callable, Mapping
from importlib import resources
from typing import Any, Protocol

import requests

from .client import RequestEncodingError, SchemaHttpClient, SerpClient
from .response import (
    ResponseError,
    SerpApiError,
    compact_serp_response,
    decode_serp_body,
    redact,
    safe_error_message,
)
from .schema import SchemaError, SchemaRepository, find_engine, iter_engines
from .serializer import (
    ParameterError,
    encode_uule_location,
    is_field_visible,
    parse_parameters_json,
    serialize_search,
)

_shared_schema_repository: SchemaRepository | None = None
_shared_schema_repository_lock = threading.Lock()


class _SchemaRepositoryProtocol(Protocol):
    def current(self) -> tuple[dict[str, Any], str]: ...


class _SerpClientProtocol(Protocol):
    api_key: str

    def request(self, form: Mapping[Any, Any]) -> tuple[int, bytes]: ...


def _get_shared_schema_repository() -> SchemaRepository:
    global _shared_schema_repository

    repository = _shared_schema_repository
    if repository is not None:
        return repository

    with _shared_schema_repository_lock:
        repository = _shared_schema_repository
        if repository is None:
            snapshot = resources.files("thordata_serp").joinpath("serp-schema.snapshot.json")
            repository = SchemaRepository(SchemaHttpClient(), snapshot, ttl=300)
            _shared_schema_repository = repository
    return repository


def _normalize_scalar_form_value(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return str(int(value)) if value.is_integer() else str(value)
    raise ParameterError("json must be a finite scalar value")


def _request_boundary[Result](
    operation: Callable[[], Result],
    *,
    failure_prefix: str,
    secret: str,
) -> Result:
    try:
        return operation()
    except requests.RequestException as error:
        failure_message = safe_error_message(error, secret)

    failure = ResponseError(f"{failure_prefix}: {failure_message}")
    raise failure from None


def _serp_request_boundary[Result](
    operation: Callable[[], Result],
    *,
    secret: str,
) -> Result:
    failure_message: str | None = None
    try:
        return _request_boundary(
            operation,
            failure_prefix="SERP request failed",
            secret=secret,
        )
    except RequestEncodingError as error:
        failure_message = safe_error_message(error, secret)

    failure = ParameterError(failure_message)
    raise failure from None


def _typed_boundary[Result](operation: Callable[[], Result], *, secret: str) -> Result:
    try:
        return operation()
    except SerpApiError as error:
        error_kind = "api"
        message = safe_error_message(error, secret)
        status_code = error.status_code
        response_data = error.response_data
    except SchemaError as error:
        error_kind = "schema"
        message = safe_error_message(error, secret)
    except ParameterError as error:
        error_kind = "parameter"
        message = safe_error_message(error, secret)
    except ResponseError as error:
        error_kind = "response"
        message = safe_error_message(error, secret)

    if error_kind == "api":
        sanitized_data = None if response_data is None else redact(response_data, secret)
        failure = SerpApiError(status_code, message, sanitized_data)
    elif error_kind == "schema":
        failure = SchemaError(message)
    elif error_kind == "parameter":
        failure = ParameterError(message)
    else:
        failure = ResponseError(message)
    raise failure from None


class ThordataSerpService:
    """Compose schema, serialization, HTTP, and response helpers."""

    def __init__(
        self,
        api_key: str,
        *,
        schema_repository: _SchemaRepositoryProtocol | None = None,
        serp_client: _SerpClientProtocol | None = None,
    ) -> None:
        if serp_client is None:
            serp_client = SerpClient(api_key)
            normalized_key = serp_client.api_key
        else:
            if not isinstance(api_key, str) or not api_key.strip():
                raise ValueError("API key must be a nonblank string")
            normalized_key = api_key.strip()
            client_key = getattr(serp_client, "api_key", None)
            if (
                not isinstance(client_key, str)
                or not client_key.strip()
                or client_key.strip() != normalized_key
            ):
                raise ValueError("injected SERP client API key must match service API key")

        self._api_key = normalized_key
        self._schema_repository = (
            schema_repository if schema_repository is not None else _get_shared_schema_repository()
        )
        self._serp_client = serp_client

    def _current_schema(self) -> tuple[dict, str]:
        return _typed_boundary(
            lambda: _request_boundary(
                self._schema_repository.current,
                failure_prefix="schema request failed",
                secret=self._api_key,
            ),
            secret=self._api_key,
        )

    def list_engines(self) -> dict:
        schema, source = self._current_schema()
        schema_engines = _typed_boundary(
            lambda: list(iter_engines(schema)),
            secret=self._api_key,
        )
        engines = [
            {
                "key": engine["key"],
                "name": engine["name"],
                "query_field": engine["query_field"],
            }
            for engine in schema_engines
        ]
        return _typed_boundary(
            lambda: redact(
                {
                    "ok": True,
                    "schema_version": schema["schema_version"],
                    "audience": schema["audience"],
                    "default_engine": schema["default_engine"],
                    "schema_source": source,
                    "engines": engines,
                },
                self._api_key,
            ),
            secret=self._api_key,
        )

    def get_engine_schema(self, engine: str) -> dict:
        if not isinstance(engine, str) or not engine.strip():
            raise SchemaError("engine must be a nonblank string")
        schema, source = self._current_schema()
        selected_engine = _typed_boundary(
            lambda: find_engine(schema, engine.strip()),
            secret=self._api_key,
        )
        return _typed_boundary(
            lambda: redact(
                {
                    "ok": True,
                    "schema_source": source,
                    "engine": selected_engine,
                },
                self._api_key,
            ),
            secret=self._api_key,
        )

    def search(
        self,
        *,
        engine: str = "",
        query: str = "",
        parameters_json: str = "{}",
        response_format: str = "1",
        response_mode: str = "compact",
    ) -> dict:
        if not isinstance(response_mode, str) or response_mode not in {"compact", "complete"}:
            raise ParameterError("response_mode must be exactly compact or complete")

        parameters = _typed_boundary(
            lambda: parse_parameters_json(parameters_json),
            secret=self._api_key,
        )
        schema, _ = self._current_schema()
        selected_engine, form = _typed_boundary(
            lambda: serialize_search(
                schema,
                engine,
                query,
                parameters,
                response_format,
            ),
            secret=self._api_key,
        )
        status, body = _typed_boundary(
            lambda: _serp_request_boundary(
                lambda: self._serp_client.request(form),
                secret=self._api_key,
            ),
            secret=self._api_key,
        )
        data = _typed_boundary(
            lambda: decode_serp_body(status, body, secret=self._api_key),
            secret=self._api_key,
        )
        if response_mode == "compact":
            data = _typed_boundary(
                lambda: compact_serp_response(data),
                secret=self._api_key,
            )
        return _typed_boundary(
            lambda: redact(
                {
                    "ok": True,
                    "status": int(status),
                    "engine": selected_engine["key"],
                    "data": data,
                },
                self._api_key,
            ),
            secret=self._api_key,
        )

    def search_fixed_engine(
        self,
        *,
        engine: str,
        query: str = "",
        parameters: Mapping[str, object] | None = None,
        extra_parameters_json: str = "{}",
        response_format: str = "1",
        response_mode: str = "compact",
    ) -> dict[str, object]:
        """Search one schema-selected engine without accepting an engine override."""
        if not isinstance(response_mode, str) or response_mode not in {"compact", "complete"}:
            raise ParameterError("response_mode must be exactly compact or complete")
        if parameters is not None and not isinstance(parameters, Mapping):
            raise ParameterError("parameters must be a mapping")
        if not isinstance(query, str):
            raise ParameterError("query must be a string")

        extra = _typed_boundary(
            lambda: parse_parameters_json(extra_parameters_json),
            secret=self._api_key,
        )
        schema, _ = self._current_schema()
        selected = _typed_boundary(
            lambda: find_engine(schema, engine),
            secret=self._api_key,
        )
        merged: dict[str, object] = dict(parameters) if parameters is not None else {}
        if query.strip():
            merged[selected["query_field"]] = query
        for key, value in extra.items():
            if key == "engine":
                continue
            merged[key] = value

        fields = {field["key"]: field for group in selected["groups"] for field in group["fields"]}

        def has_value(value: object) -> bool:
            if value is None:
                return False
            if isinstance(value, str):
                return bool(value.strip())
            if isinstance(value, (list, dict)):
                return bool(value)
            return True

        effective_values = dict(merged)
        for key, field in fields.items():
            if (
                key not in effective_values
                and field.get("visible") is not False
                and "default_value" in field
                and has_value(field.get("default_value"))
            ):
                effective_values[key] = field["default_value"]
        location_field = fields.get("location")
        uule_field = fields.get("uule")
        location = effective_values.get("location")
        explicit_uule = effective_values.get("uule")
        supports_uule = (
            isinstance(location_field, Mapping)
            and isinstance(uule_field, Mapping)
            and is_field_visible(location_field, effective_values, fields)
            and is_field_visible(uule_field, effective_values, fields)
        )
        has_explicit_uule = has_value(explicit_uule)
        if (
            supports_uule
            and not has_explicit_uule
            and isinstance(location, str)
            and location.strip()
        ):
            merged["uule"] = encode_uule_location(location)

        selected_engine, form = _typed_boundary(
            lambda: serialize_search(
                schema,
                engine,
                "",
                merged,
                response_format,
            ),
            secret=self._api_key,
        )
        extra_json = extra.get("json")
        if has_value(extra_json):
            if not isinstance(extra_json, (str, int, float, bool)):
                raise ParameterError("json must be a scalar value")
            form["json"] = _normalize_scalar_form_value(extra_json)
        status, body = _typed_boundary(
            lambda: _serp_request_boundary(
                lambda: self._serp_client.request(form),
                secret=self._api_key,
            ),
            secret=self._api_key,
        )
        data = _typed_boundary(
            lambda: decode_serp_body(status, body, secret=self._api_key),
            secret=self._api_key,
        )
        if response_mode == "compact":
            data = _typed_boundary(
                lambda: compact_serp_response(data),
                secret=self._api_key,
            )
        return _typed_boundary(
            lambda: redact(
                {
                    "ok": True,
                    "status": int(status),
                    "engine": selected_engine["key"],
                    "data": data,
                },
                self._api_key,
            ),
            secret=self._api_key,
        )


def build_service(api_key: str) -> ThordataSerpService:
    """Build a service with a shared schema repository and a fresh SERP client."""
    return ThordataSerpService(
        api_key,
        schema_repository=_get_shared_schema_repository(),
    )
