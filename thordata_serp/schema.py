"""Validation and loading utilities for Thordata SERP schema documents."""

import json
import math
import threading
import time
from collections.abc import Callable, Iterator
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol

from .json_safety import validate_json_tree

SUPPORTED_CONTROLS = {
    "input": "string",
    "number": "number",
    "select": "options",
    "multi_select": "array",
    "switch": "boolean",
    "json": "object",
}
SUPPORTED_CONDITION_OPERATORS = {"equals", "not_equals", "in", "not_in"}
SUPPORTED_VALIDATION_RULES = {"required_any_of", "mutually_exclusive"}
RESERVED_QUERY_FIELDS = {"engine", "json", "isjson"}
_DANGEROUS_KEYS = frozenset({"__proto__", "prototype", "constructor"})

_N8N_FIELD_TYPES = frozenset(SUPPORTED_CONTROLS.values())
_SUPPORTED_FIELD_TYPES = frozenset(
    {
        "array",
        "boolean",
        "cascader",
        "date_range",
        "number",
        "object",
        "options",
        "string",
        "switch",
        "tags",
        "time_range",
    }
)
_QUERY_FIELD_TYPES = frozenset({"options", "string"})
_RESERVED_SERIALIZER_KEYS = frozenset(RESERVED_QUERY_FIELDS)
_SHOW_WHEN_OPERATORS = frozenset(SUPPORTED_CONDITION_OPERATORS)


class SchemaError(RuntimeError):
    """Raised when a SERP schema document is invalid."""


class SchemaClientProtocol(Protocol):
    """Minimal interface required to fetch a remote schema document."""

    def fetch(self) -> dict[str, Any]:
        """Fetch a schema response envelope or direct schema object."""


class SnapshotResource(Protocol):
    """Minimal resource interface for bundled schema snapshots."""

    def read_text(self, *, encoding: str) -> str:
        """Read the resource as text using the requested encoding."""


@dataclass
class _SchemaRefresh:
    done: bool = False
    schema: dict[str, Any] | None = None
    source: str | None = None
    failure_message: str | None = None


class SchemaRepository:
    """Load validated schemas with bounded caching and single-flight refreshes."""

    def __init__(
        self,
        client: SchemaClientProtocol,
        snapshot_path: SnapshotResource,
        ttl: float = 300,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            isinstance(ttl, bool)
            or not isinstance(ttl, (int, float))
            or not ttl > 0
            or isinstance(ttl, float)
            and not math.isfinite(ttl)
        ):
            raise ValueError("ttl must be a positive non-boolean number")

        self._client = client
        self._snapshot_path = snapshot_path
        self._ttl = ttl
        self._clock = clock
        self._condition = threading.Condition()
        self._schema: dict[str, Any] | None = None
        self._loaded_at: float | None = None
        self._refresh: _SchemaRefresh | None = None

    def current(self) -> tuple[dict[str, Any], str]:
        """Return the current valid schema and the source that supplied it."""
        result_schema: dict[str, Any] | None = None
        result_source: str | None = None
        with self._condition:
            try:
                is_fresh = self._is_fresh()
            except Exception as clock_error:
                raise SchemaError(
                    f"schema cache clock failed: {_error_text(clock_error)}"
                ) from None
            if is_fresh:
                result_schema = self._schema
                result_source = "cache"
            elif self._refresh is not None:
                refresh = self._refresh
                while not refresh.done:
                    self._condition.wait()
                if refresh.failure_message is not None:
                    raise SchemaError(refresh.failure_message) from None
                if refresh.schema is not None and refresh.source is not None:
                    result_schema = refresh.schema
                    result_source = refresh.source
                else:
                    raise SchemaError("schema refresh completed without a result")
            else:
                refresh = _SchemaRefresh()
                self._refresh = refresh

        if result_schema is not None and result_source is not None:
            return deepcopy(result_schema), result_source

        schema: dict[str, Any] | None = None
        source: str | None = None
        loaded_at: float | None = None
        error: SchemaError | None = None
        try:
            try:
                schema = validate_schema_payload(self._client.fetch())
                source = "remote"
            except Exception as remote_error:
                with self._condition:
                    stale_schema = self._schema

                if stale_schema is not None:
                    schema = stale_schema
                    source = "cache"
                else:
                    try:
                        schema = load_snapshot(self._snapshot_path)
                        source = "snapshot"
                    except Exception as snapshot_error:
                        error = SchemaError(
                            "remote schema fetch failed: "
                            f"{_error_text(remote_error)}; schema snapshot fallback failed: "
                            f"{_error_text(snapshot_error)}"
                        )
            if error is None and source in {"remote", "snapshot"}:
                try:
                    loaded_at = self._clock()
                except Exception as clock_error:
                    error = SchemaError(
                        f"schema cache timestamp failed: {_error_text(clock_error)}"
                    )
        except Exception as unexpected_error:
            error = SchemaError(f"schema refresh failed: {_error_text(unexpected_error)}")
        finally:
            with self._condition:
                if error is None and schema is not None and source is not None:
                    if source in {"remote", "snapshot"}:
                        self._schema = schema
                        self._loaded_at = loaded_at
                    refresh.schema = schema
                    refresh.source = source
                    refresh.failure_message = None
                else:
                    error = error or SchemaError("schema refresh failed without a result")
                    refresh.schema = None
                    refresh.source = None
                    refresh.failure_message = str(error)
                refresh.done = True
                if self._refresh is refresh:
                    self._refresh = None
                self._condition.notify_all()

        if refresh.failure_message is not None:
            raise SchemaError(refresh.failure_message) from None
        if refresh.schema is None or refresh.source is None:
            raise SchemaError("schema refresh failed without a result")
        return deepcopy(refresh.schema), refresh.source

    def _is_fresh(self) -> bool:
        return (
            self._schema is not None
            and self._loaded_at is not None
            and self._clock() - self._loaded_at < self._ttl
        )


def _error_text(error: Exception) -> str:
    try:
        text = str(error)
    except Exception:
        return type(error).__name__
    return " ".join(text.split()) or type(error).__name__


def _nonblank(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _safe_key(value: Any) -> bool:
    return _nonblank(value) and value not in _DANGEROUS_KEYS


def _reject_dangerous_keys(value: Any) -> None:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            for key, child in current.items():
                if key in _DANGEROUS_KEYS:
                    raise SchemaError("schema contains a dangerous key")
                pending.append(child)
        elif isinstance(current, list):
            pending.extend(current)


def _optional_strings(value: dict[str, Any], keys: tuple[str, ...]) -> None:
    for key in keys:
        if key in value and not isinstance(value[key], str):
            raise SchemaError(f"{key} must be a string")


def _scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool))


def _same_scalar(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    return type(left) is type(right) and left == right


def _option_values(options: list[dict[str, Any]]) -> list[Any]:
    values: list[Any] = []
    pending = list(options)
    while pending:
        option = pending.pop()
        values.append(option["value"])
        children = option.get("children")
        if children is not None:
            if not isinstance(children, list):
                raise SchemaError("field option children must be an array")
            pending.extend(children)
    return values


def _validate_options(options: Any) -> list[dict[str, Any]]:
    if not isinstance(options, list) or not options:
        raise SchemaError("field options must be a nonempty array")
    pending = [options]
    normalized: list[dict[str, Any]] = []
    while pending:
        current = pending.pop()
        if not isinstance(current, list) or not current:
            raise SchemaError("field options must be a nonempty array")
        for option in current:
            if not isinstance(option, dict):
                raise SchemaError("field options require scalar labels and values")
            if any(key in _DANGEROUS_KEYS for key in option):
                raise SchemaError("field options contain a dangerous key")
            if set(option) != {"label", "value"}:
                raise SchemaError("field options contain unsupported keys")
            if not _nonblank(option.get("label")) or not _scalar(option.get("value")):
                raise SchemaError("field options require scalar labels and values")
            normalized.append(option)
            children = option.get("children")
            if children is not None:
                raise SchemaError("field options do not support children")
    seen: list[Any] = []
    for option in normalized:
        if any(_same_scalar(option["value"], value) for value in seen):
            raise SchemaError("field options must not contain duplicate values")
        seen.append(option["value"])
    return normalized


def _default_matches(field_type: str, value: Any, option_values: list[Any]) -> bool:
    if field_type == "string":
        return isinstance(value, str)
    if field_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if field_type == "boolean":
        return isinstance(value, bool)
    if field_type == "object":
        return isinstance(value, dict)
    if field_type == "array":
        return isinstance(value, list) and all(_scalar(item) for item in value)
    if field_type == "options":
        return _scalar(value) and any(_same_scalar(value, option) for option in option_values)
    return False


def _validate_field(
    field: dict[str, Any],
    *,
    require_control: bool = True,
    is_query_field: bool = False,
) -> None:
    field_type = field.get("type")
    if not _nonblank(field_type):
        raise SchemaError("field type must be a nonblank string")
    if field_type not in _N8N_FIELD_TYPES:
        raise SchemaError(f"unsupported field type: {field_type}")
    control = field.get("control")
    if not isinstance(control, str) or control not in SUPPORTED_CONTROLS:
        raise SchemaError("field control must be supported")
    if SUPPORTED_CONTROLS[control] != field_type:
        if is_query_field:
            raise SchemaError("query_field must reference a query-compatible type")
        raise SchemaError("field control does not match field type")
    if not _nonblank(field.get("label")):
        raise SchemaError("field label must be a nonblank string")
    _optional_strings(field, ("name", "group", "description", "placeholder"))
    for key in ("visible", "required"):
        if key not in field or not isinstance(field[key], bool):
            raise SchemaError(f"field {key} must be a boolean")
    if field.get("visible") is False and field.get("required") is True:
        raise SchemaError(
            "query_field must be visible; required cannot be true when field is hidden"
        )

    option_values: list[Any] = []
    if control in {"select", "multi_select"}:
        options = _validate_options(field.get("options"))
        option_values = _option_values(options)
    elif "options" in field:
        raise SchemaError("field options are only valid for select controls")

    for key in ("default", "default_value"):
        if key not in field or field[key] is None:
            continue
        value = field[key]
        try:
            validate_json_tree(value)
        except ValueError as error:
            raise SchemaError(f"field {key} has invalid JSON value: {error}") from error
        if field_type == "array":
            valid = _default_matches(field_type, value, option_values) and (
                not option_values
                or all(
                    any(_same_scalar(item, option) for option in option_values) for item in value
                )
            )
        else:
            valid = _default_matches(field_type, value, option_values)
        if not valid:
            raise SchemaError(f"field {key} has invalid shape")


def _raw_engines(schema: dict[str, Any]) -> list[dict[str, Any]]:
    engines: list[dict[str, Any]] = []
    categories = schema.get("categories", [])
    if not isinstance(categories, list):
        raise SchemaError("categories must be an array")

    category_keys: set[str] = set()
    for category in categories:
        if not isinstance(category, dict):
            raise SchemaError("category must be an object")
        category_key = category.get("key")
        if not _safe_key(category_key):
            raise SchemaError("category key must be nonblank")
        if category_key in category_keys:
            raise SchemaError("duplicate category key")
        category_keys.add(category_key)
        if not _nonblank(category.get("name")):
            raise SchemaError("category name must be a nonblank string")

        category_engines = category.get("engines")
        if not isinstance(category_engines, list):
            raise SchemaError("category engines must be an array")
        engines.extend(category_engines)

    direct_engines = schema.get("engines", [])
    if not isinstance(direct_engines, list):
        raise SchemaError("engines must be an array")
    engines.extend(direct_engines)
    return engines


def validate_schema_payload(payload: Any) -> dict[str, Any]:
    """Validate a schema response envelope or direct schema object."""
    if not isinstance(payload, dict):
        raise SchemaError("schema payload must be an object")
    try:
        validate_json_tree(payload)
    except ValueError as error:
        raise SchemaError(f"invalid schema JSON: {error}") from error
    _reject_dangerous_keys(payload)

    if "code" in payload and not _is_success_code(payload["code"]):
        code = payload["code"]
        for detail_key in ("msg", "message", "error"):
            detail = payload.get(detail_key)
            if detail is not None:
                raise SchemaError(f"schema payload business code {code}: {detail}")
        raise SchemaError(f"schema payload has unsuccessful business code: {code}")

    schema = payload.get("data", payload)
    if not isinstance(schema, dict):
        raise SchemaError("schema data must be an object")

    if not _nonblank(schema.get("schema_version")):
        raise SchemaError("schema_version must be nonblank")
    if schema.get("audience") != "is_serp_old=0":
        raise SchemaError("unsupported audience")
    if not _safe_key(schema.get("default_engine")):
        raise SchemaError("default engine must be nonblank")

    engines = _raw_engines(schema)
    engine_keys: set[str] = set()
    for engine in engines:
        if not isinstance(engine, dict):
            raise SchemaError("engine must be an object")
        engine_key = engine.get("key")
        if not _safe_key(engine_key):
            raise SchemaError("engine key must be nonblank")
        if engine_key in engine_keys:
            raise SchemaError("duplicate engine key")
        engine_keys.add(engine_key)
        if not _nonblank(engine.get("name")):
            raise SchemaError("engine name must be nonblank")
        if not _safe_key(engine.get("query_field")):
            raise SchemaError("engine query_field must be nonblank")

        validation_rules = engine.get("validation_rules")
        if validation_rules == []:
            validation_rules = None
        if isinstance(validation_rules, list):
            validation_rule_list = validation_rules
        elif validation_rules is None:
            validation_rule_list = []
        else:
            raise SchemaError("engine validation_rules must be an array")
        for validation_rule in validation_rule_list:
            if not isinstance(validation_rule, dict):
                raise SchemaError("engine validation_rules entries must be objects")
            rule_type = validation_rule.get("type")
            if rule_type not in SUPPORTED_VALIDATION_RULES:
                raise SchemaError("engine validation_rules.type is unsupported")
            rule_fields = validation_rule.get("fields")
            if (
                not isinstance(rule_fields, list)
                or len(rule_fields) < 2
                or any(not _safe_key(field_key) for field_key in rule_fields)
                or len(set(rule_fields)) != len(rule_fields)
            ):
                raise SchemaError("engine validation_rules.fields must contain distinct field keys")

        groups = engine.get("groups")
        if not isinstance(groups, list):
            raise SchemaError("engine groups must be an array")

        group_keys: set[str] = set()
        field_keys: set[str] = set()
        fields_by_key: dict[str, dict[str, Any]] = {}
        fields: list[dict[str, Any]] = []
        for group in groups:
            if not isinstance(group, dict):
                raise SchemaError("group must be an object")
            group_key = group.get("key")
            if not _safe_key(group_key):
                raise SchemaError("group key must be nonblank")
            if group_key in group_keys:
                raise SchemaError("duplicate group key")
            group_keys.add(group_key)
            if not _nonblank(group.get("name")):
                raise SchemaError("group name must be a nonblank string")

            group_fields = group.get("fields")
            if not isinstance(group_fields, list):
                raise SchemaError("group fields must be an array")
            for field in group_fields:
                if not isinstance(field, dict):
                    raise SchemaError("field must be an object")
                field_key = field.get("key")
                if not _safe_key(field_key):
                    raise SchemaError("field key must be nonblank")
                if field_key in field_keys:
                    raise SchemaError("duplicate field key")
                field_keys.add(field_key)
                _validate_field(
                    field,
                    require_control=field_key == engine["query_field"],
                    is_query_field=field_key == engine["query_field"],
                )
                fields_by_key[field_key] = field
                fields.append(field)

        if engine["query_field"] not in field_keys:
            raise SchemaError("engine query_field must reference a declared field")
        query_field = fields_by_key[engine["query_field"]]
        if engine["query_field"] in _RESERVED_SERIALIZER_KEYS:
            raise SchemaError("engine query_field must not use a reserved serializer key")
        if query_field["type"] not in _QUERY_FIELD_TYPES:
            raise SchemaError("engine query_field must reference a query-compatible type")
        if query_field.get("visible") is False:
            raise SchemaError("engine query_field must be visible")
        if "show_when" in query_field:
            raise SchemaError("engine query_field must not declare show_when")

        for validation_rule in validation_rule_list:
            for field_key in validation_rule["fields"]:
                if field_key not in fields_by_key:
                    raise SchemaError(
                        "engine validation_rules.fields must reference declared fields"
                    )

        for field in fields:
            if "show_when" not in field:
                continue
            show_when = field["show_when"]
            if not isinstance(show_when, dict):
                raise SchemaError("show_when must be an object")
            if not _nonblank(show_when.get("field")):
                raise SchemaError("show_when.field must be a nonblank field key")
            if (
                not _safe_key(show_when.get("field"))
                or show_when["field"] not in field_keys
                or show_when["field"] == field["key"]
            ):
                raise SchemaError("show_when.field must reference a field in the same engine")
            if set(show_when) != {"field", "operator", "values"}:
                raise SchemaError("show_when contains unsupported keys")
            operator = show_when.get("operator")
            if not isinstance(operator, str):
                raise SchemaError("show_when.operator must be a string")
            if operator not in _SHOW_WHEN_OPERATORS:
                raise SchemaError(f"show_when.operator is unsupported: {operator}")
            values = show_when.get("values")
            if (
                not isinstance(values, list)
                or not values
                or not all(_scalar(value) or value is None for value in values)
            ):
                raise SchemaError("show_when.values must be a nonempty array of scalars")

    if schema["default_engine"] not in engine_keys:
        raise SchemaError("default engine must be declared")
    return deepcopy(schema)


def _is_success_code(value: Any) -> bool:
    if value is None:
        return True
    if type(value) is int:
        return value in (0, 200)
    if type(value) is str:
        return value in ("0", "200")
    return False


def iter_engines(schema: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield copies of category engines followed by direct engines."""
    for engine in _raw_engines(schema):
        yield deepcopy(engine)


def find_engine(schema: dict[str, Any], key: str) -> dict[str, Any]:
    """Return a copy of the engine identified by *key*."""
    for engine in _raw_engines(schema):
        if engine.get("key") == key:
            return deepcopy(engine)
    raise SchemaError(f"unknown engine: {key}")


def load_snapshot(path: SnapshotResource) -> dict[str, Any]:
    """Load and validate a UTF-8 JSON schema snapshot."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise SchemaError(f"cannot load schema snapshot: {_error_text(error)}") from error
    return validate_schema_payload(payload)
