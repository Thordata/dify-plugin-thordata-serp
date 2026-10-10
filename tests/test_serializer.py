import math
import sys
from collections import UserDict
from copy import deepcopy

import pytest

from thordata_serp.schema import _SUPPORTED_FIELD_TYPES, SchemaError, validate_schema_payload
from thordata_serp.serializer import (
    _SERIALIZED_FIELD_TYPES,
    ParameterError,
    parse_parameters_json,
    serialize_search,
)


@pytest.fixture
def serializer_schema(schema_payload):
    payload = deepcopy(schema_payload)
    payload["data"]["categories"][0]["engines"][0]["groups"][0]["fields"][0]["required"] = False
    schema = validate_schema_payload(payload)
    fields = schema["categories"][0]["engines"][0]["groups"][0]["fields"]
    fields.extend(
        [
            {"key": "text", "type": "string"},
            {
                "key": "mode",
                "type": "options",
                "options": [
                    {"label": "Basic", "value": "basic"},
                    {
                        "label": "More",
                        "value": "more",
                        "children": [
                            {"label": "Advanced", "value": "advanced"},
                            {"label": "Expert", "value": "expert"},
                        ],
                    },
                ],
            },
            {
                "key": "numeric_mode",
                "type": "options",
                "options": [{"label": "One", "value": 1}],
            },
            {"key": "safe", "type": "boolean"},
            {"key": "toggle", "type": "switch"},
            {"key": "tags", "type": "array", "separator": ";"},
            {"key": "labels", "type": "tags"},
            {"key": "cr", "type": "tags"},
            {"key": "country", "type": "string", "control": "country"},
            {"key": "config", "type": "object"},
            {"key": "dates", "type": "date_range"},
            {"key": "dates_start", "type": "string"},
            {"key": "hours", "type": "time_range"},
            {"key": "place", "type": "cascader"},
            {
                "key": "equals_field",
                "type": "string",
                "show_when": {"field": "mode", "operator": "equals", "values": ["advanced"]},
            },
            {
                "key": "not_equals_field",
                "type": "string",
                "show_when": {
                    "field": "mode",
                    "operator": "not_equals",
                    "values": ["basic"],
                },
            },
            {
                "key": "in_field",
                "type": "string",
                "show_when": {"field": "mode", "operator": "in", "values": ["advanced"]},
            },
            {
                "key": "not_in_field",
                "type": "string",
                "show_when": {"field": "mode", "operator": "not_in", "values": ["basic"]},
            },
            {
                "key": "query_condition",
                "type": "string",
                "show_when": {"field": "q", "operator": "equals", "values": ["top"]},
            },
            {
                "key": "boolean_condition",
                "type": "string",
                "show_when": {"field": "safe", "operator": "equals", "values": [True]},
            },
            {
                "key": "country_condition",
                "type": "string",
                "show_when": {"field": "country", "operator": "equals", "values": ["US"]},
            },
            {
                "key": "number_condition",
                "type": "string",
                "show_when": {"field": "num", "operator": "equals", "values": [1]},
            },
            {
                "key": "numeric_option_condition",
                "type": "string",
                "show_when": {
                    "field": "numeric_mode",
                    "operator": "equals",
                    "values": [1],
                },
            },
            {"key": "invisible", "type": "string", "visible": False},
        ]
    )
    return schema


@pytest.mark.parametrize("value", [None, "", "  \t\r\n"])
def test_empty_parameters_json_is_an_empty_object(value):
    assert parse_parameters_json(value) == {}


@pytest.mark.parametrize("value", [{}, [], 1, True])
def test_parameters_json_requires_a_string(value):
    with pytest.raises(ParameterError, match="parameters_json must be a JSON object string"):
        parse_parameters_json(value)


def test_parameters_json_must_be_an_object():
    with pytest.raises(ParameterError, match="JSON object"):
        parse_parameters_json('["not", "an", "object"]')


@pytest.mark.parametrize("value", ["{", '{"unsafe": NaN}', '{"unsafe": "\\ud800"}'])
def test_parameters_json_normalizes_invalid_json_tree_errors(value):
    with pytest.raises(ParameterError, match="^parameters_json must contain valid JSON$") as caught:
        parse_parameters_json(value)

    assert "unsafe" not in str(caught.value)


def test_parameters_json_rejects_excessive_depth_without_leaking_tree_details():
    value = '{"secret":' + "[" * 257 + "0" + "]" * 257 + "}"

    with pytest.raises(ParameterError, match="^parameters_json must contain valid JSON$") as caught:
        parse_parameters_json(value)

    assert "secret" not in str(caught.value)


def test_parameters_json_normalizes_oversized_integer_conversion_errors():
    get_limit = getattr(sys, "get_int_max_str_digits", None)
    if not callable(get_limit):
        pytest.skip("the interpreter does not expose the integer conversion limit")
    limit = get_limit()
    if limit <= 0:
        pytest.skip("the interpreter integer conversion limit is disabled")
    value = '{"secret":' + "9" * (limit + 1) + "}"

    with pytest.raises(ParameterError, match="^parameters_json must contain valid JSON$") as caught:
        parse_parameters_json(value)

    assert caught.value.__cause__ is None
    assert "secret" not in str(caught.value)


def test_parameters_json_preserves_unicode_and_returns_a_fresh_object():
    assert parse_parameters_json('{"query":"café","nested":{"ok":true}}') == {
        "query": "café",
        "nested": {"ok": True},
    }


def test_serializer_and_schema_supported_types_stay_synchronized():
    assert _SERIALIZED_FIELD_TYPES == _SUPPORTED_FIELD_TYPES


def test_serializes_every_supported_type_and_top_level_values(serializer_schema):
    engine, form = serialize_search(
        serializer_schema,
        " google ",
        "authoritative query",
        {
            "q": "ignored query",
            "engine": "bing",
            "json": "3",
            "num": 0,
            "text": False,
            "mode": "advanced",
            "safe": False,
            "toggle": "yes",
            "tags": ["a", 2, True],
            "labels": ["x", 1.5, False],
            "cr": ["US", "country gb", "Canada", "zz"],
            "country": " united kingdom ",
            "config": {"greeting": "你好", "enabled": False},
            "dates": {"start": "2026-01-01", "end": "2026-01-31"},
            "hours": [9, 5, 18, 30],
            "place": ["us", "ny", "nyc"],
            "unknown": "ignored",
        },
        "1",
    )

    assert engine["key"] == "google"
    assert form == {
        "q": "authoritative query",
        "num": "0",
        "text": "false",
        "mode": "advanced",
        "safe": "false",
        "toggle": "true",
        "tags": "a;2;true",
        "labels": "x,1.5,false",
        "cr": "countryUS|countryGB|countryCA|countryZZ",
        "country": "GB",
        "config": '{"greeting":"你好","enabled":false}',
        "dates_start": "2026-01-01",
        "dates_end": "2026-01-31",
        "hours": "0905,1830",
        "place": "nyc",
        "json": "1",
        "engine": "google",
    }


def test_default_engine_and_blank_query_preserve_parameter_query(serializer_schema):
    engine, form = serialize_search(serializer_schema, "  ", "  ", {"q": "tea"}, "2")

    assert engine["key"] == "google"
    assert form == {"q": "tea", "json": "2", "engine": "google"}


def test_nonblank_query_preserves_surrounding_spaces(serializer_schema):
    _, form = serialize_search(serializer_schema, "google", "  coffee  ", {}, "3")

    assert form["q"] == "  coffee  "


def test_effective_query_controls_visibility_and_overrides_invalid_nested_query(serializer_schema):
    params = {"q": ["invalid"], "query_condition": "shown"}

    _, form = serialize_search(serializer_schema, "google", "top", params, "1")

    assert form["q"] == "top"
    assert form["query_condition"] == "shown"
    assert params == {"q": ["invalid"], "query_condition": "shown"}


@pytest.mark.parametrize("value", [None, 1, True, [], {}])
def test_non_string_engine_is_rejected(serializer_schema, value):
    with pytest.raises(ParameterError, match="engine_key must be a string"):
        serialize_search(serializer_schema, value, "", {}, "1")


@pytest.mark.parametrize("value", [None, 1, True, [], {}])
def test_non_string_query_is_rejected(serializer_schema, value):
    with pytest.raises(ParameterError, match="query must be a string"):
        serialize_search(serializer_schema, "google", value, {}, "1")


@pytest.mark.parametrize("value", [None, 1, 2, "", "0", "4", True])
def test_response_format_must_be_one_two_or_three(serializer_schema, value):
    with pytest.raises(ParameterError, match="response_format must be one of: 1, 2, 3"):
        serialize_search(serializer_schema, "google", "", {}, value)


def test_unknown_engine_raises_schema_error(serializer_schema):
    with pytest.raises(SchemaError, match="unknown engine"):
        serialize_search(serializer_schema, "missing", "", {}, "1")


def test_params_accept_mapping_and_reject_non_mapping(serializer_schema):
    _, form = serialize_search(serializer_schema, "google", "", UserDict({"num": 3}), "1")
    assert form["num"] == "3"

    with pytest.raises(ParameterError, match="params must be a mapping"):
        serialize_search(serializer_schema, "google", "", [("num", 3)], "1")


@pytest.mark.parametrize("value", [None, "", "  ", [], {}])
def test_empty_values_are_omitted(serializer_schema, value):
    params = {
        key: value
        for key in ("q", "text", "safe", "num", "tags", "config", "dates", "hours", "place")
    }

    _, form = serialize_search(serializer_schema, "google", "", params, "1")

    assert form == {"json": "1", "engine": "google"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, "true"),
        (False, "false"),
        (1, "true"),
        (0, "false"),
        (" TRUE ", "true"),
        ("1", "true"),
        ("yes", "true"),
        ("Y", "true"),
        ("on", "true"),
        ("false", "false"),
        ("0", "false"),
        ("No", "false"),
        ("n", "false"),
        ("OFF", "false"),
    ],
)
def test_boolean_and_switch_use_documented_representations(serializer_schema, value, expected):
    _, form = serialize_search(
        serializer_schema, "google", "", {"safe": value, "toggle": value}, "1"
    )

    assert form["safe"] == expected
    assert form["toggle"] == expected


@pytest.mark.parametrize("value", [2, -1, "truthy", [1], {"value": True}])
def test_boolean_rejects_undocumented_representations(serializer_schema, value):
    with pytest.raises(ParameterError, match="safe.*boolean"):
        serialize_search(serializer_schema, "google", "", {"safe": value}, "1")


@pytest.mark.parametrize("value", [True, "1", math.nan, math.inf, -math.inf])
def test_number_requires_a_finite_non_boolean_number(serializer_schema, value):
    with pytest.raises(ParameterError, match="num.*finite number"):
        serialize_search(serializer_schema, "google", "", {"num": value}, "1")


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"text": [1]}, "text.*scalar"),
        ({"tags": {"bad": 1}}, "tags.*array"),
        ({"tags": ["ok", {"bad": 1}]}, "tags.*scalar"),
        ({"place": {"bad": 1}}, "place.*array"),
        ({"config": [1]}, "config.*object"),
        ({"dates": [1]}, "dates.*date range object"),
        ({"dates": {"start": "2026-01-01"}}, "dates.*start.*end"),
        ({"hours": [9, 18]}, "hours.*four"),
        ({"hours": [True, 5, 18, 30]}, "hours.*integer"),
        ({"hours": [24, 5, 18, 30]}, "hours.*hour.*0 through 23"),
        ({"hours": [9, 60, 18, 30]}, "hours.*minute.*0 through 59"),
    ],
)
def test_malformed_values_have_key_specific_errors(serializer_schema, params, message):
    with pytest.raises(ParameterError, match=message):
        serialize_search(serializer_schema, "google", "", params, "1")


def test_object_requires_a_safe_json_tree(serializer_schema):
    recursive = {}
    recursive["self"] = recursive

    for value in (recursive, {"number": math.nan}, {1: "bad"}):
        with pytest.raises(ParameterError, match="config.*JSON object") as caught:
            serialize_search(serializer_schema, "google", "", {"config": value}, "1")
        assert "number" not in str(caught.value)


@pytest.mark.parametrize(
    "params",
    [
        {
            "dates": {"start": "2026-01-01", "end": "2026-01-31"},
            "dates_start": "manual",
        },
        {
            "dates_start": "manual",
            "dates": {"start": "2026-01-01", "end": "2026-01-31"},
        },
    ],
)
def test_date_range_output_collisions_are_rejected_independent_of_parameter_order(
    serializer_schema, params
):
    with pytest.raises(ParameterError, match="collision.*dates_start"):
        serialize_search(serializer_schema, "google", "", params, "1")


def test_nested_options_are_flattened_and_arbitrary_scalar_values_are_preserved(serializer_schema):
    _, form = serialize_search(serializer_schema, "google", "", {"mode": "expert"}, "1")
    assert form["mode"] == "expert"

    _, form = serialize_search(serializer_schema, "google", "", {"mode": "missing"}, "1")
    assert form["mode"] == "missing"


def test_country_aliases_and_prefixed_codes_are_normalized(serializer_schema):
    _, form = serialize_search(
        serializer_schema,
        "google",
        "",
        {
            "cr": [
                "country United States",
                "USA",
                "uk",
                "United Kingdom",
                "Canada",
                "COUNTRY australia",
                "China",
                "country zz",
            ]
        },
        "1",
    )

    assert form["cr"] == (
        "countryUS|countryUS|countryGB|countryGB|countryCA|countryAU|countryCN|countryZZ"
    )


def test_singular_country_options_accept_aliases_and_emit_plain_codes(serializer_schema):
    fields = serializer_schema["categories"][0]["engines"][0]["groups"][0]["fields"]
    country = next(field for field in fields if field["key"] == "country")
    country["type"] = "options"
    country["options"] = [
        {"label": "United States", "value": "us"},
        {"label": "United Kingdom", "value": "gb"},
    ]

    _, form = serialize_search(serializer_schema, "google", "", {"country": "USA"}, "1")

    assert form["country"] == "US"


@pytest.mark.parametrize(
    ("operator", "mode", "visible"),
    [
        ("equals", "advanced", True),
        ("equals", "basic", False),
        ("not_equals", "advanced", True),
        ("not_equals", "basic", False),
        ("in", "advanced", True),
        ("in", "basic", False),
        ("not_in", "advanced", True),
        ("not_in", "basic", False),
    ],
)
def test_show_when_operators_use_effective_input_values(serializer_schema, operator, mode, visible):
    field_key = {
        "equals": "equals_field",
        "not_equals": "not_equals_field",
        "in": "in_field",
        "not_in": "not_in_field",
    }[operator]

    _, form = serialize_search(
        serializer_schema, "google", "", {"mode": mode, field_key: "value"}, "1"
    )

    assert (field_key in form) is visible


def test_show_when_normalizes_boolean_aliases_before_comparison(serializer_schema):
    _, form = serialize_search(
        serializer_schema,
        "google",
        "",
        {"safe": "true", "boolean_condition": "shown"},
        "1",
    )

    assert form["safe"] == "true"
    assert form["boolean_condition"] == "shown"


def test_show_when_normalizes_country_aliases_before_comparison(serializer_schema):
    _, form = serialize_search(
        serializer_schema,
        "google",
        "",
        {"country": "USA", "country_condition": "shown"},
        "1",
    )

    assert form["country"] == "US"
    assert form["country_condition"] == "shown"


def test_show_when_preserves_number_int_float_equivalence(serializer_schema):
    _, form = serialize_search(
        serializer_schema,
        "google",
        "",
        {"num": 1.0, "number_condition": "shown"},
        "1",
    )

    assert form["num"] == "1.0"
    assert form["number_condition"] == "shown"


def test_show_when_preserves_numeric_option_int_float_equivalence(serializer_schema):
    _, form = serialize_search(
        serializer_schema,
        "google",
        "",
        {"numeric_mode": 1.0, "numeric_option_condition": "shown"},
        "1",
    )

    assert form["numeric_mode"] == "1.0"
    assert form["numeric_option_condition"] == "shown"


def test_show_when_and_explicitly_invisible_fields_are_omitted_without_controllers(
    serializer_schema,
):
    _, form = serialize_search(
        serializer_schema,
        "google",
        "",
        {
            "equals_field": "hidden",
            "not_equals_field": "hidden",
            "in_field": "hidden",
            "not_in_field": "hidden",
            "invisible": "hidden",
        },
        "1",
    )

    assert not set(form).intersection(
        {"equals_field", "not_equals_field", "in_field", "not_in_field", "invisible"}
    )


def test_unsupported_condition_operator_omits_field(serializer_schema):
    fields = serializer_schema["categories"][0]["engines"][0]["groups"][0]["fields"]
    field = next(item for item in fields if item["key"] == "equals_field")
    field["show_when"]["operator"] = "unknown"

    _, form = serialize_search(
        serializer_schema,
        "google",
        "",
        {"mode": "advanced", "equals_field": "hidden"},
        "1",
    )

    assert "equals_field" not in form


def test_flights_airport_ids_are_stripped_and_uppercased(schema_payload):
    engine = schema_payload["data"]["categories"][0]["engines"][0]
    engine["key"] = "google_flights"
    engine["query_field"] = "departure_id"
    engine["groups"][0]["fields"][0]["key"] = "departure_id"
    engine["groups"][0]["fields"].append(
        {
            "key": "arrival_id",
            "label": "Arrival Airport",
            "type": "string",
            "control": "input",
            "visible": True,
            "required": False,
        }
    )
    schema_payload["data"]["default_engine"] = "google_flights"
    schema = validate_schema_payload(schema_payload)

    _, form = serialize_search(schema, "google_flights", " lax ", {"arrival_id": " jfk "}, "1")

    assert form["departure_id"] == "LAX"
    assert form["arrival_id"] == "JFK"


def test_source_schema_params_and_returned_engine_are_independent(serializer_schema):
    schema = deepcopy(serializer_schema)
    params = {"config": {"a": [1]}, "place": ["us", "ny", "nyc"]}
    original_schema = deepcopy(schema)
    original_params = deepcopy(params)

    engine, _ = serialize_search(schema, "google", "", params, "2")
    engine["groups"][0]["fields"][0]["key"] = "changed"

    assert schema == original_schema
    assert params == original_params


def test_unsupported_schema_field_type_is_rejected(serializer_schema):
    serializer_schema["categories"][0]["engines"][0]["groups"][0]["fields"].append(
        {"key": "unsupported", "type": "unknown"}
    )

    with pytest.raises(ParameterError, match="unsupported.*unsupported schema type"):
        serialize_search(serializer_schema, "google", "", {"unsupported": "value"}, "1")


def _integer_beyond_string_conversion_limit():
    get_limit = getattr(sys, "get_int_max_str_digits", None)
    if not callable(get_limit):
        pytest.skip("the interpreter does not support configurable integer string digit limits")
    limit = get_limit()
    if limit <= 0:
        pytest.skip("the interpreter has no integer string digit limit")
    value = 10**limit
    with pytest.raises(ValueError):
        str(value)
    return value


@pytest.mark.parametrize(
    ("params", "message"),
    [
        (lambda value: {"num": value}, "num.*conversion"),
        (lambda value: {"tags": [value]}, "tags.*conversion"),
    ],
)
def test_huge_integer_conversion_errors_are_parameter_errors(serializer_schema, params, message):
    value = _integer_beyond_string_conversion_limit()

    with pytest.raises(ParameterError, match=message):
        serialize_search(serializer_schema, "google", "", params(value), "1")


def test_google_web_multi_values_use_n8n_pipe_joining(schema_payload):
    engine = schema_payload["data"]["categories"][0]["engines"][0]
    engine["key"] = "google_web"
    schema_payload["data"]["default_engine"] = "google_web"
    fields = engine["groups"][0]["fields"]
    fields.extend(
        [
            {
                "key": "cr",
                "label": "Countries",
                "type": "array",
                "control": "multi_select",
                "visible": True,
                "required": False,
                "options": [
                    {"label": "United States", "value": "countryUS"},
                    {"label": "United Kingdom", "value": "countryGB"},
                ],
            },
            {
                "key": "lr",
                "label": "Languages",
                "type": "array",
                "control": "multi_select",
                "visible": True,
                "required": False,
                "options": [
                    {"label": "English", "value": "en"},
                    {"label": "French", "value": "fr"},
                ],
            },
        ]
    )
    schema = validate_schema_payload(schema_payload)

    _, form = serialize_search(
        schema,
        "google_web",
        "coffee",
        {"cr": ["US", "GB"], "lr": ["en", "fr"]},
        "1",
    )

    assert form["cr"] == "countryUS|countryGB"
    assert form["lr"] == "en|fr"


def test_visible_default_value_is_serialized_and_validation_rules_are_enforced(schema_payload):
    engine = schema_payload["data"]["categories"][0]["engines"][0]
    fields = engine["groups"][0]["fields"]
    fields[1]["default_value"] = 10
    fields.extend(
        [
            {
                "key": "first",
                "label": "First",
                "type": "string",
                "control": "input",
                "visible": True,
                "required": False,
            },
            {
                "key": "second",
                "label": "Second",
                "type": "string",
                "control": "input",
                "visible": True,
                "required": False,
            },
        ]
    )
    engine["validation_rules"] = [
        {"type": "required_any_of", "fields": ["first", "second"]},
        {"type": "mutually_exclusive", "fields": ["first", "second"]},
    ]
    schema = validate_schema_payload(schema_payload)

    _, form = serialize_search(schema, "google", "coffee", {"first": "one"}, "1")
    assert form["num"] == "10"

    with pytest.raises(ParameterError, match="required_any_of"):
        serialize_search(schema, "google", "coffee", {}, "1")
    with pytest.raises(ParameterError, match="mutually_exclusive"):
        serialize_search(schema, "google", "coffee", {"first": "one", "second": "two"}, "1")


def test_visible_required_field_must_be_present_after_defaults_and_query(schema_payload):
    fields = schema_payload["data"]["categories"][0]["engines"][0]["groups"][0]["fields"]
    fields.append(
        {
            "key": "required_text",
            "label": "Required Text",
            "type": "string",
            "control": "input",
            "visible": True,
            "required": True,
        }
    )
    schema = validate_schema_payload(schema_payload)

    with pytest.raises(ParameterError, match="SERP engine google required field required_text"):
        serialize_search(schema, "google", "coffee", {}, "1")


def test_required_any_of_counts_false_as_present_and_mutually_exclusive_removes_false_aliases(
    schema_payload,
):
    engine = schema_payload["data"]["categories"][0]["engines"][0]
    fields = engine["groups"][0]["fields"]
    fields[0]["required"] = False
    fields.extend(
        [
            {
                "key": "required_flag",
                "label": "Required Flag",
                "type": "boolean",
                "control": "switch",
                "visible": True,
                "required": False,
            },
            {
                "key": "exclusive_flag",
                "label": "Exclusive Flag",
                "type": "boolean",
                "control": "switch",
                "visible": True,
                "required": False,
            },
            {
                "key": "exclusive_mode",
                "label": "Exclusive Mode",
                "type": "options",
                "control": "select",
                "visible": True,
                "required": False,
                "options": [
                    {"label": "Basic", "value": "basic"},
                    {"label": "Advanced", "value": "advanced"},
                ],
            },
        ]
    )
    engine["validation_rules"] = [
        {"type": "required_any_of", "fields": ["required_flag", "q"]},
        {"type": "mutually_exclusive", "fields": ["exclusive_flag", "exclusive_mode"]},
    ]
    schema = validate_schema_payload(schema_payload)

    _, form = serialize_search(
        schema,
        "google",
        "",
        {"required_flag": False, "exclusive_flag": "off", "exclusive_mode": "advanced"},
        "1",
    )

    assert form["required_flag"] == "false"
    assert "exclusive_flag" not in form
    assert form["exclusive_mode"] == "advanced"


def test_explicit_blank_value_tombstones_visible_default(schema_payload):
    schema_payload["data"]["categories"][0]["engines"][0]["groups"][0]["fields"][1][
        "default_value"
    ] = 10
    schema = validate_schema_payload(schema_payload)

    _, form = serialize_search(schema, "google", "coffee", {"num": ""}, "1")

    assert "num" not in form


def test_conditional_hidden_fields_do_not_satisfy_required_any_of(schema_payload):
    engine = schema_payload["data"]["categories"][0]["engines"][0]
    fields = engine["groups"][0]["fields"]
    fields[0]["required"] = False
    fields.extend(
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
                "key": "conditional_text",
                "label": "Conditional Text",
                "type": "string",
                "control": "input",
                "visible": True,
                "required": False,
                "show_when": {"field": "mode", "operator": "equals", "values": ["advanced"]},
            },
            {
                "key": "fallback_text",
                "label": "Fallback Text",
                "type": "string",
                "control": "input",
                "visible": True,
                "required": False,
            },
        ]
    )
    engine["validation_rules"] = [
        {"type": "required_any_of", "fields": ["conditional_text", "fallback_text"]}
    ]
    schema = validate_schema_payload(schema_payload)

    with pytest.raises(ParameterError, match="required_any_of"):
        serialize_search(
            schema,
            "google",
            "coffee",
            {"mode": "basic", "conditional_text": "hidden"},
            "1",
        )


def test_array_values_accept_trimmed_comma_and_json_array_strings(serializer_schema):
    _, form = serialize_search(
        serializer_schema,
        "google",
        "coffee",
        {"tags": " a, b ", "labels": '[" x ", "y"]'},
        "1",
    )

    assert form["tags"] == "a;b"
    assert form["labels"] == "x,y"


def test_false_like_float_values_match_n8n_mutual_and_required_rules(schema_payload):
    engine = schema_payload["data"]["categories"][0]["engines"][0]
    fields = engine["groups"][0]["fields"]
    fields[0]["required"] = False
    fields.extend(
        [
            {
                "key": "required_flag",
                "label": "Required Flag",
                "type": "boolean",
                "control": "switch",
                "visible": True,
                "required": False,
            },
            {
                "key": "exclusive_flag",
                "label": "Exclusive Flag",
                "type": "boolean",
                "control": "switch",
                "visible": True,
                "required": False,
            },
            {
                "key": "exclusive_mode",
                "label": "Exclusive Mode",
                "type": "options",
                "control": "select",
                "visible": True,
                "required": False,
                "options": [
                    {"label": "Disabled", "value": 0.0},
                    {"label": "Enabled", "value": 1.0},
                ],
            },
        ]
    )
    engine["validation_rules"] = [
        {"type": "required_any_of", "fields": ["required_flag", "q"]},
        {"type": "mutually_exclusive", "fields": ["exclusive_flag", "exclusive_mode"]},
    ]
    schema = validate_schema_payload(schema_payload)

    _, form = serialize_search(
        schema,
        "google",
        "coffee",
        {"required_flag": 0.0, "exclusive_flag": 1.0, "exclusive_mode": 0.0},
        "1",
    )

    assert form["required_flag"] == "false"
    assert form["exclusive_flag"] == "true"
    assert "exclusive_mode" not in form


@pytest.mark.parametrize("value", ["no", "n"])
def test_mutually_exclusive_options_keep_no_and_n_active(schema_payload, value):
    engine = schema_payload["data"]["categories"][0]["engines"][0]
    fields = engine["groups"][0]["fields"]
    fields[0]["required"] = False
    fields.extend(
        [
            {
                "key": "first_mode",
                "label": "First Mode",
                "type": "options",
                "control": "select",
                "visible": True,
                "required": False,
                "options": [
                    {"label": "No", "value": "no"},
                ],
            },
            {
                "key": "second_mode",
                "label": "Second Mode",
                "type": "options",
                "control": "select",
                "visible": True,
                "required": False,
                "options": [{"label": "Enabled", "value": "enabled"}],
            },
        ]
    )
    engine["validation_rules"] = [
        {"type": "mutually_exclusive", "fields": ["first_mode", "second_mode"]}
    ]
    schema = validate_schema_payload(schema_payload)

    _, form = serialize_search(
        schema,
        "google",
        "coffee",
        {"first_mode": value},
        "1",
    )

    assert form["first_mode"] == value


def test_array_controller_conditions_trim_string_values(serializer_schema):
    fields = serializer_schema["categories"][0]["engines"][0]["groups"][0]["fields"]
    fields.append(
        {
            "key": "array_condition_target",
            "label": "Array Condition Target",
            "type": "string",
            "control": "input",
            "visible": True,
            "required": False,
            "show_when": {"field": "tags", "operator": "equals", "values": ["a"]},
        }
    )

    _, form = serialize_search(
        serializer_schema,
        "google",
        "coffee",
        {"tags": " a ", "array_condition_target": "shown"},
        "1",
    )

    assert form["array_condition_target"] == "shown"


def test_non_query_strings_and_options_are_trimmed_but_query_is_preserved(serializer_schema):
    _, form = serialize_search(
        serializer_schema,
        "google",
        "  coffee  ",
        {"mode": "  advanced  ", "text": "  notes  "},
        "1",
    )

    assert form["q"] == "  coffee  "
    assert form["mode"] == "advanced"
    assert form["text"] == "notes"


def test_show_when_option_conditions_trim_controller_strings(serializer_schema):
    fields = serializer_schema["categories"][0]["engines"][0]["groups"][0]["fields"]
    fields.append(
        {
            "key": "trimmed_condition_target",
            "label": "Trimmed Condition Target",
            "type": "string",
            "control": "input",
            "visible": True,
            "required": False,
            "show_when": {"field": "mode", "operator": "equals", "values": ["advanced"]},
        }
    )

    _, form = serialize_search(
        serializer_schema,
        "google",
        "coffee",
        {"mode": "  advanced  ", "trimmed_condition_target": "shown"},
        "1",
    )

    assert form["trimmed_condition_target"] == "shown"


def test_null_array_entries_are_rejected(serializer_schema):
    with pytest.raises(ParameterError, match="tags.*scalar"):
        serialize_search(serializer_schema, "google", "coffee", {"tags": [None]}, "1")


def test_required_conditional_field_is_validated_when_hidden(schema_payload):
    engine = schema_payload["data"]["categories"][0]["engines"][0]
    fields = engine["groups"][0]["fields"]
    fields[0]["required"] = False
    fields.extend(
        [
            {
                "key": "mode",
                "label": "Mode",
                "type": "options",
                "control": "select",
                "visible": True,
                "required": False,
                "options": [{"label": "Basic", "value": "basic"}],
            },
            {
                "key": "conditional_required",
                "label": "Conditional Required",
                "type": "string",
                "control": "input",
                "visible": True,
                "required": True,
                "show_when": {"field": "mode", "operator": "equals", "values": ["advanced"]},
            },
        ]
    )
    schema = validate_schema_payload(schema_payload)

    with pytest.raises(ParameterError, match="required field conditional_required"):
        serialize_search(schema, "google", "coffee", {"mode": "basic"}, "1")
