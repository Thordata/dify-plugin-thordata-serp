import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from thordata_serp.json_safety import validate_json_tree  # noqa: E402


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        1,
        1.5,
        "text",
        [1, "two", None],
        {"items": [{"ok": True}]},
    ],
)
def test_validate_json_tree_accepts_json_values(value: object) -> None:
    assert validate_json_tree(value) is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_validate_json_tree_rejects_non_finite_numbers(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        validate_json_tree({"value": value})


def test_validate_json_tree_rejects_non_string_object_keys() -> None:
    with pytest.raises(ValueError, match="keys must be strings"):
        validate_json_tree({1: "value"})


@pytest.mark.parametrize("value", [{"value": b"bytes"}, {"value": object()}, (1, 2)])
def test_validate_json_tree_rejects_unsupported_python_values(value: object) -> None:
    with pytest.raises(ValueError, match="not a JSON type"):
        validate_json_tree(value)


@pytest.mark.parametrize("container_type", ["dict", "list"])
def test_validate_json_tree_rejects_cycles(container_type: str) -> None:
    value: dict[str, object] | list[object] = {} if container_type == "dict" else []
    if isinstance(value, dict):
        value["self"] = value
    else:
        value.append(value)

    with pytest.raises(ValueError, match="cycle"):
        validate_json_tree(value)


def test_validate_json_tree_allows_reused_noncyclic_containers() -> None:
    shared = {"value": 1}

    assert validate_json_tree([shared, shared]) is None


def test_validate_json_tree_rejects_nesting_beyond_256_without_recursion() -> None:
    root: list[object] = []
    current = root
    for _ in range(max(257, sys.getrecursionlimit() + 10)):
        nested: list[object] = []
        current.append(nested)
        current = nested

    with pytest.raises(ValueError, match="nesting exceeds 256"):
        validate_json_tree(root)


@pytest.mark.parametrize("value", ["\ud800", {"\udfff": "value"}])
def test_validate_json_tree_rejects_invalid_unicode(value: object) -> None:
    with pytest.raises(ValueError, match="Unicode"):
        validate_json_tree(value)
