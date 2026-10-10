"""Schema-directed serialization for Thordata SERP search parameters."""

import base64
import json
import math
from collections.abc import Mapping
from typing import Any

from .json_safety import validate_json_tree
from .schema import find_engine

_SERIALIZED_FIELD_TYPES = frozenset(
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
_BOOLEAN_TRUE = frozenset({"1", "on", "true", "y", "yes"})
_BOOLEAN_FALSE = frozenset({"0", "false", "n", "no", "off"})
_COUNTRY_ALIASES = {
    "US": "US",
    "USA": "US",
    "UNITED STATES": "US",
    "GB": "GB",
    "UK": "GB",
    "UNITED KINGDOM": "GB",
    "CA": "CA",
    "CANADA": "CA",
    "AU": "AU",
    "AUSTRALIA": "AU",
    "CN": "CN",
    "CHINA": "CN",
}
_UULE_LENGTH_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


class ParameterError(ValueError):
    """Raised when a search parameter cannot be serialized."""


def parse_parameters_json(value: object) -> dict[str, object]:
    """Parse the Dify JSON-string parameter boundary into an object."""
    if value is None or isinstance(value, str) and not value.strip():
        return {}
    if not isinstance(value, str):
        raise ParameterError("parameters_json must be a JSON object string")
    try:
        parsed = json.loads(value)
    except (ValueError, OverflowError, RecursionError):
        raise ParameterError("parameters_json must contain valid JSON") from None
    if not isinstance(parsed, dict):
        raise ParameterError("parameters_json must contain a JSON object")
    try:
        validate_json_tree(parsed)
    except (ValueError, RecursionError):
        raise ParameterError("parameters_json must contain valid JSON") from None
    return parsed


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def _scalar(value: Any, key: str) -> str | int | float | bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ParameterError(f"{key} must be a finite scalar value")


def _scalar_text(value: Any, key: str) -> str:
    scalar = _scalar(value, key)
    if isinstance(scalar, bool):
        return "true" if scalar else "false"
    try:
        return str(scalar)
    except (OverflowError, ValueError):
        raise ParameterError(f"{key} scalar conversion failed") from None


def _same_scalar(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    return type(left) is type(right) and left == right


def _boolean_text(value: Any, key: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value in (0, 1):
        return "true" if value == 1 else "false"
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _BOOLEAN_TRUE:
            return "true"
        if normalized in _BOOLEAN_FALSE:
            return "false"
    raise ParameterError(f"{key} must be a boolean")


def _number_text(value: Any, key: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ParameterError(f"{key} must be a finite number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ParameterError(f"{key} must be a finite number")
    try:
        return str(value)
    except (OverflowError, ValueError):
        raise ParameterError(f"{key} number conversion failed") from None


def _array_items(value: Any, key: str) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        normalized = value.strip()
        if normalized.startswith("["):
            try:
                parsed = json.loads(normalized)
            except (ValueError, OverflowError, RecursionError):
                raise ParameterError(f"{key} must be an array of scalar values") from None
            if not isinstance(parsed, list):
                raise ParameterError(f"{key} must be an array of scalar values")
            return parsed
        return normalized.split(",")
    raise ParameterError(f"{key} must be an array of scalar values")


def _scalar_list(value: Any, key: str) -> list[str]:
    result: list[str] = []
    for item in _array_items(value, key):
        if item is None:
            raise ParameterError(f"{key} must be an array of scalar values")
        if isinstance(item, str) and not item.strip():
            continue
        result.append(_scalar_text(item, key).strip())
    return result


def _is_boolean_false_like(value: Any) -> bool:
    if value is False:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 0
    return isinstance(value, str) and value.strip().lower() in _BOOLEAN_FALSE


def _is_option_false_like(value: Any) -> bool:
    if value is False:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 0
    return isinstance(value, str) and value.strip().lower() in {"false", "0", "off"}


def _country_code(value: Any, key: str) -> str:
    country = _scalar_text(value, key).strip()
    if country.lower().startswith("country"):
        country = country[7:].strip()
    if not country:
        raise ParameterError(f"{key} must contain a country value")
    normalized = country.upper()
    return _COUNTRY_ALIASES.get(normalized, normalized)


def _country_token(value: Any, key: str) -> str:
    return f"country{_country_code(value, key)}"


def _is_country_field(field: Mapping[str, Any]) -> bool:
    control = field.get("control")
    return field["key"] in {"country", "cr"} or (
        isinstance(control, str) and control.strip().lower() == "country"
    )


def _separator(field: Mapping[str, Any]) -> str:
    separator = field.get("separator")
    return separator if isinstance(separator, str) and separator else ","


def _serialize_date_range(value: Any, key: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ParameterError(f"{key} must be a date range object")
    start = value.get("start")
    end = value.get("end")
    if not _present(start) or not _present(end):
        raise ParameterError(f"{key} requires start and end values")
    return {
        f"{key}_start": _scalar_text(start, key),
        f"{key}_end": _scalar_text(end, key),
    }


def _serialize_time_range(value: Any, key: str) -> str:
    if not isinstance(value, list) or len(value) != 4:
        raise ParameterError(f"{key} must contain four time components")
    if any(isinstance(component, bool) or not isinstance(component, int) for component in value):
        raise ParameterError(f"{key} time components must be integers")
    start_hour, start_minute, end_hour, end_minute = value
    if not 0 <= start_hour <= 23 or not 0 <= end_hour <= 23:
        raise ParameterError(f"{key} hour values must be from 0 through 23")
    if not 0 <= start_minute <= 59 or not 0 <= end_minute <= 59:
        raise ParameterError(f"{key} minute values must be from 0 through 59")
    return f"{start_hour:02d}{start_minute:02d},{end_hour:02d}{end_minute:02d}"


def _serialize_cascader(field: Mapping[str, Any], value: Any, key: str) -> str:
    values = _scalar_list(value, key)
    if not values:
        raise ParameterError(f"{key} must be a nonempty cascader")
    separator = field.get("separator")
    if isinstance(separator, str) and separator:
        return separator.join(values)
    return values[-1]


def _serialize_object(value: Any, key: str) -> str:
    if not isinstance(value, dict):
        raise ParameterError(f"{key} must be an object")
    try:
        validate_json_tree(value)
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ParameterError(f"{key} must be a JSON object") from None


def _serialize_scalar_field(
    field: Mapping[str, Any], value: Any, engine_key: str, query_field: str
) -> str:
    key = field["key"]
    field_type = field.get("type")
    if field_type in {"boolean", "switch"}:
        return _boolean_text(value, key)
    if field_type == "number":
        return _number_text(value, key)
    text = _scalar_text(value, key)
    if field_type in {"string", "options"} and key != query_field:
        text = text.strip()
    if _is_country_field(field):
        country_code = _country_code(value, key)
        return f"country{country_code}" if key == "cr" else country_code
    if engine_key == "google_flights" and key in {"departure_id", "arrival_id"}:
        return text.strip().upper()
    return text


def _serialize_field(
    field: Mapping[str, Any], value: Any, engine_key: str, query_field: str
) -> dict[str, str]:
    key = field["key"]
    field_type = field.get("type")
    if field_type not in _SERIALIZED_FIELD_TYPES:
        raise ParameterError(f"{key} has unsupported schema type: {field_type}")
    if field_type in {"boolean", "switch", "number", "string", "options"}:
        return {key: _serialize_scalar_field(field, value, engine_key, query_field)}
    if field_type in {"array", "tags"}:
        items = _array_items(value, key)
        values = _scalar_list(value, key)
        if _is_country_field(field):
            serialized = [_country_token(item, key) for item in items if _present(item)]
        else:
            serialized = values
        if key in {"cr", "lr"}:
            return {key: "|".join(serialized)}
        return {key: _separator(field).join(serialized)}
    if field_type == "object":
        return {key: _serialize_object(value, key)}
    if field_type == "date_range":
        return _serialize_date_range(value, key)
    if field_type == "time_range":
        return {key: _serialize_time_range(value, key)}
    if field_type == "cascader":
        return {key: _serialize_cascader(field, value, key)}
    raise ParameterError(f"{key} has unsupported schema type: {field_type}")


def _fields(engine: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {field["key"]: field for group in engine["groups"] for field in group["fields"]}


def _condition_scalar(field: Mapping[str, Any], value: Any) -> Any:
    field_type = field.get("type")
    key = field["key"]
    if field_type in {"boolean", "switch"}:
        return _boolean_text(value, key)
    if field_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ParameterError(f"{key} must be a finite number")
        if isinstance(value, float) and not math.isfinite(value):
            raise ParameterError(f"{key} must be a finite number")
        return value
    if _is_country_field(field):
        return _country_code(value, key)
    if field_type in {"string", "options"}:
        scalar = _scalar(value, key)
        return scalar.strip() if isinstance(scalar, str) else scalar
    if field_type in {"array", "tags", "cascader"}:
        return _scalar(value, key)
    return value


def _condition_candidates(field: Mapping[str, Any], value: Any) -> list[Any]:
    field_type = field.get("type")
    if field_type in {"array", "tags"}:
        items = _array_items(value, field["key"])
        return [
            _condition_scalar(field, item.strip() if isinstance(item, str) else item)
            for item in items
            if item is not None and (not isinstance(item, str) or item.strip())
        ]
    if field_type == "cascader":
        if not isinstance(value, list):
            raise ParameterError(f"{field['key']} must be an array of scalar values")
        if not value:
            raise ParameterError(f"{field['key']} must be a nonempty cascader")
        return [_condition_scalar(field, value[-1])]
    return [_condition_scalar(field, value)]


def _condition_matches(
    condition: Mapping[str, Any],
    values: Mapping[str, Any],
    fields: Mapping[str, Mapping[str, Any]],
) -> bool:
    controller = condition.get("field")
    if not isinstance(controller, str) or controller not in values or controller not in fields:
        return False
    actual = values[controller]
    if not _present(actual):
        return False
    expected = condition.get("values")
    if not isinstance(expected, list) or not expected:
        return False
    controller_field = fields[controller]
    candidates = _condition_candidates(controller_field, actual)
    normalized_expected: list[Any] = []
    for option in expected:
        try:
            normalized_expected.append(_condition_scalar(controller_field, option))
        except ParameterError:
            continue
    matched = any(
        _same_scalar(candidate, option)
        for candidate in candidates
        for option in normalized_expected
    )
    operator = condition.get("operator", "equals")
    if operator in {"equals", "in"}:
        return matched
    if operator in {"not_equals", "not_in"}:
        return not matched
    return False


def _field_visible(
    field: Mapping[str, Any],
    values: Mapping[str, Any],
    fields: Mapping[str, Mapping[str, Any]],
) -> bool:
    if field.get("visible") is False:
        return False
    condition = field.get("show_when")
    return not isinstance(condition, dict) or _condition_matches(condition, values, fields)


def is_field_visible(
    field: Mapping[str, Any],
    values: Mapping[str, Any],
    fields: Mapping[str, Mapping[str, Any]],
) -> bool:
    """Return whether a schema field is visible for the effective values."""
    return _field_visible(field, values, fields)


def _output_keys(field: Mapping[str, Any]) -> tuple[str, ...]:
    key = field["key"]
    if field.get("type") == "date_range":
        return (f"{key}_start", f"{key}_end")
    return (key,)


def _field_present(field: Mapping[str, Any], value: Any) -> bool:
    if field.get("type") in {"array", "tags"}:
        try:
            return bool(_scalar_list(value, field["key"]))
        except ParameterError:
            return _present(value)
    return _present(value)


def _accepted_fields(
    fields: Mapping[str, dict[str, Any]],
    values: Mapping[str, Any],
    engine_key: str,
) -> list[tuple[dict[str, Any], Any]]:
    accepted: list[tuple[dict[str, Any], Any]] = []
    output_owners: dict[str, str] = {}
    for key, field in fields.items():
        if key in {"engine", "json", "isjson"}:
            continue
        if field.get("required") is True and not _field_present(field, values.get(key)):
            raise ParameterError(f"SERP engine {engine_key} required field {key} is missing")
        if key not in values or not _field_present(field, values[key]):
            continue
        if not _field_visible(field, values, fields):
            continue
        for output_key in _output_keys(field):
            owner = output_owners.get(output_key)
            if owner is not None and owner != key:
                raise ParameterError(
                    f"output key collision: {output_key} is produced by {owner} and {key}"
                )
            output_owners[output_key] = key
        accepted.append((field, values[key]))
    return accepted


def _rule_present(
    field: Mapping[str, Any] | None,
    value: Any,
    *,
    discount_disabled: bool = False,
) -> bool:
    if not _present(value):
        return False
    if field is None or not discount_disabled:
        return True
    field_type = field.get("type")
    if field_type in {"boolean", "switch"} and _is_boolean_false_like(value):
        return False
    if field_type == "options":
        if _is_option_false_like(value):
            return False
        if isinstance(value, str):
            return value.strip().lower() not in {"false", "0", "off"}
    return True


def _validate_engine_rules(
    engine: Mapping[str, Any],
    fields: Mapping[str, Mapping[str, Any]],
    values: Mapping[str, Any],
) -> None:
    rules = engine.get("validation_rules")
    if not isinstance(rules, list):
        return
    for rule in rules:
        if not isinstance(rule, Mapping):
            continue
        rule_type = rule.get("type")
        rule_fields = rule.get("fields")
        if rule_type not in {"required_any_of", "mutually_exclusive"} or not isinstance(
            rule_fields, list
        ):
            continue
        present_count = sum(
            _rule_present(
                fields.get(key),
                values.get(key),
                discount_disabled=rule_type == "mutually_exclusive",
            )
            for key in rule_fields
            if isinstance(key, str)
        )
        failed = rule_type == "required_any_of" and present_count == 0
        failed = failed or rule_type == "mutually_exclusive" and present_count > 1
        if failed:
            joined = ", ".join(str(key) for key in rule_fields)
            raise ParameterError(
                f"SERP engine {engine['key']} validation {rule_type} failed for fields {joined}"
            )


def _remove_disabled_mutually_exclusive_values(
    engine: Mapping[str, Any],
    fields: Mapping[str, Mapping[str, Any]],
    values: dict[str, Any],
) -> None:
    rules = engine.get("validation_rules")
    if not isinstance(rules, list):
        return
    for rule in rules:
        if not isinstance(rule, Mapping) or rule.get("type") != "mutually_exclusive":
            continue
        rule_fields = rule.get("fields")
        if not isinstance(rule_fields, list):
            continue
        for key in rule_fields:
            field = fields.get(key)
            value = values.get(key)
            if field is None:
                continue
            field_type = field.get("type")
            if (
                field_type in {"boolean", "switch"}
                and _is_boolean_false_like(value)
                or field_type == "options"
                and _is_option_false_like(value)
            ):
                values.pop(key, None)


def encode_uule_location(location: str) -> str:
    """Encode a location using n8n's Latin-1 UULE representation."""
    normalized = location.strip()
    if normalized == "":
        return ""
    try:
        raw = bytes(ord(character) for character in normalized)
    except ValueError:
        raise ParameterError("UULE location must contain Latin-1 characters") from None
    length_character = _UULE_LENGTH_ALPHABET[len(normalized) % len(_UULE_LENGTH_ALPHABET)]
    encoded = base64.b64encode(raw).decode("ascii").rstrip("=")
    return f"w+CAIQICI{length_character}{encoded}"


def serialize_search(
    schema: dict[str, Any],
    engine_key: object,
    query: object,
    params: Mapping[str, Any] | None,
    response_format: object,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Serialize search inputs according to the selected engine's field schema."""
    if not isinstance(engine_key, str):
        raise ParameterError("engine_key must be a string")
    if not isinstance(query, str):
        raise ParameterError("query must be a string")
    if not isinstance(response_format, str) or response_format not in {"1", "2", "3"}:
        raise ParameterError("response_format must be one of: 1, 2, 3")

    selected_key = engine_key.strip() or schema["default_engine"]
    engine = find_engine(schema, selected_key)
    if params is not None and not isinstance(params, Mapping):
        raise ParameterError("params must be a mapping")

    effective_values = dict(params) if params is not None else {}
    fields = _fields(engine)
    for key, field in fields.items():
        if (
            field.get("visible") is not False
            and "default_value" in field
            and _present(field.get("default_value"))
            and key not in effective_values
        ):
            effective_values[key] = field["default_value"]
    if query.strip():
        effective_values[engine["query_field"]] = query

    _remove_disabled_mutually_exclusive_values(engine, fields, effective_values)
    form: dict[str, str] = {}
    accepted = _accepted_fields(fields, effective_values, engine["key"])
    visible_values = {field["key"]: value for field, value in accepted}
    _validate_engine_rules(engine, fields, visible_values)
    for field, value in accepted:
        form.update(_serialize_field(field, value, engine["key"], engine["query_field"]))

    form["json"] = response_format
    form["engine"] = engine["key"]
    return engine, form
