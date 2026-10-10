"""Generate deterministic fixed-engine Dify tools from the validated SERP schema."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))
SNAPSHOT_PATH = SOURCE_ROOT / "thordata_serp" / "serp-schema.snapshot.json"
GENERATED_DIR = SOURCE_ROOT / "tools" / "generated"
PROVIDER_PATH = SOURCE_ROOT / "provider" / "thordata_serp.yaml"


@dataclass(frozen=True)
class GeneratedToolSet:
    tool_names: tuple[str, ...]
    provider_tool_paths: tuple[str, ...]


class GeneratorError(ValueError):
    """Raised when a validated schema cannot be emitted safely as source files."""


def _validated_schema(schema: Mapping[str, object]) -> dict[str, Any]:
    from thordata_serp.schema import validate_schema_payload

    payload = dict(schema)
    data = payload.get("data", payload)
    if isinstance(data, dict):
        engines: list[object] = list(data.get("engines", []))
        for category in data.get("categories", []):
            if not isinstance(category, dict):
                continue
            engines.extend(category.get("engines", []))
        for engine in engines:
            if not isinstance(engine, dict):
                continue
            _validate_generated_key(engine.get("key"), "engine key")
            _validate_generated_key(
                engine.get("query_field"), f"engine {engine.get('key', '<unknown>')} query_field"
            )
    try:
        return validate_schema_payload(payload)
    except GeneratorError:
        raise
    except Exception as error:
        raise GeneratorError(f"schema validation failed: {error}") from error


def load_generation_schema(*, use_snapshot: bool, api_key: str | None = None) -> dict[str, object]:
    """Load a snapshot or explicitly opt into the public remote schema endpoint.

    The current schema endpoint does not require authentication.  ``api_key`` is
    therefore an explicit readiness gate for remote generation only; it is never
    sent to the endpoint, logged, or included in generated files.
    """
    if use_snapshot:
        payload = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        return _validated_schema(payload)
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("api_key is required for remote generation")
    from thordata_serp.client import SchemaHttpClient

    return _validated_schema(SchemaHttpClient().fetch())


def _safe_name(key: str, used: set[str]) -> str:
    candidate = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_") or "engine"
    if candidate[0].isdigit():
        candidate = f"engine_{candidate}"
    if candidate not in used:
        used.add(candidate)
        return candidate
    prefixed = f"thordata_{candidate}"
    if prefixed not in used:
        used.add(prefixed)
        return prefixed
    index = 2
    while f"{prefixed}_{index}" in used:
        index += 1
    result = f"{prefixed}_{index}"
    used.add(result)
    return result


_SAFE_GENERATED_KEY = re.compile(r"[a-z0-9_-]{1,255}\Z")
_SAFE_PARAMETER_KEY = re.compile(r"[A-Za-z0-9_-]{1,255}\Z")
_RESERVED_GENERATED_FIELDS = frozenset(
    {
        "engine",
        "json",
        "isjson",
        "extra_parameters_json",
        "response_format",
        "response_mode",
    }
)


def _validate_generated_key(value: object, path: str) -> str:
    if not isinstance(value, str) or _SAFE_GENERATED_KEY.fullmatch(value) is None:
        raise GeneratorError(f"{path} must be a safe identifier matching [a-z0-9_-]{{1,255}}")
    return value


def _validate_parameter_key(value: object, path: str) -> str:
    if not isinstance(value, str) or _SAFE_PARAMETER_KEY.fullmatch(value) is None:
        raise GeneratorError(
            f"{path} must be a safe parameter name matching [A-Za-z0-9_-]{{1,255}}"
        )
    return value


def _engine_records(schema: dict[str, Any]) -> list[tuple[dict[str, Any], str, str]]:
    from thordata_serp.schema import iter_engines

    used: set[str] = set()
    records: list[tuple[dict[str, Any], str, str]] = []
    for engine in iter_engines(schema):
        key = _validate_generated_key(engine["key"], "engine key")
        _validate_generated_key(engine["query_field"], f"engine {key} query_field")
        if key in _RESERVED_GENERATED_FIELDS:
            raise GeneratorError(f"engine {key} uses a reserved generated tool key")
        for group in engine["groups"]:
            for field in group["fields"]:
                field_key = field["key"]
                _validate_parameter_key(field_key, f"engine {key} parameter name")
                if field_key in _RESERVED_GENERATED_FIELDS - {"json", "isjson"} and field.get(
                    "visible"
                ):
                    raise GeneratorError(
                        f"engine {key} field {field_key} conflicts with a generated parameter"
                    )
                if field.get("control") == "multi_select":
                    values = [option["value"] for option in field.get("options", [])]
                    default = field.get("default_value")
                    if isinstance(default, list):
                        values.extend(default)
                    if any(not isinstance(value, str) for value in values):
                        raise GeneratorError(
                            f"engine {key} array field {field_key} has non-string values"
                        )
        safe = _safe_name(key, used)
        category_name = ""
        for category in schema.get("categories", []):
            if isinstance(category, dict) and any(
                isinstance(candidate, dict) and candidate.get("key") == key
                for candidate in category.get("engines", [])
            ):
                category_name = str(category.get("name", ""))
                break
        records.append((engine, safe, category_name))
    return records


def generate_provider_tool_paths(schema: Mapping[str, object]) -> list[str]:
    validated = _validated_schema(schema)
    return [f"tools/generated/{safe}.yaml" for _, safe, _ in _engine_records(validated)]


def _i18n(value: object) -> dict[str, str]:
    return {"en_US": str(value)}


def _supports_uule(engine: Mapping[str, Any]) -> bool:
    fields = {
        field["key"]: field
        for group in engine.get("groups", [])
        for field in group.get("fields", [])
        if isinstance(field, Mapping)
    }
    return (
        fields.get("location", {}).get("visible") is True
        and fields.get("uule", {}).get("visible") is True
    )


def _field_parameter(
    field: dict[str, Any], query_field: str, *, supports_uule: bool = False
) -> dict[str, Any]:
    control = field["control"]
    field_type = field["type"]
    name = str(field["key"])
    if control in {"select", "multi_select"}:
        parameter_type = "dynamic-select"
    elif control == "switch":
        parameter_type = "boolean"
    elif control == "json":
        parameter_type = "object"
    else:
        parameter_type = str(field_type)

    description = str(field.get("description") or field.get("label") or name)
    if name == "uule":
        description = (
            "Optional encoded location; leave empty and it is auto-derived "
            "when Location is selected."
        )
    elif name == "location" and supports_uule:
        description = f"{description} UULE is derived automatically from the selected location."
    parameter: dict[str, Any] = {
        "name": name,
        "type": parameter_type,
        "required": bool(field["required"]),
        "label": _i18n(field["label"]),
        "human_description": _i18n(description),
        "llm_description": description,
        "form": "llm" if name == query_field else "form",
    }
    if "placeholder" in field:
        parameter["placeholder"] = _i18n(field["placeholder"])
    if control == "multi_select":
        default = field.get("default_value")
        parameter["default"] = list(default) if isinstance(default, list) else []
    elif field.get("default_value") is not None:
        parameter["default"] = field["default_value"]
    if parameter_type == "object":
        parameter["input_schema"] = {"type": "object", "additionalProperties": True}
    if parameter_type == "dynamic-select":
        parameter["multiple"] = control == "multi_select"
        parameter["options"] = [
            {
                "value": str(option["value"]),
                "label": _i18n(option["label"]),
            }
            for option in field.get("options", [])
        ]
    if parameter_type == "number":
        for bound in ("min", "max"):
            value = field.get(bound)
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            ):
                parameter[bound] = value
    show_when = field.get("show_when")
    if isinstance(show_when, dict) and show_when.get("operator") == "equals":
        parameter["show_on"] = [
            {"variable": str(show_when["field"]), "value": str(value)}
            for value in show_when.get("values", [])
        ]
    return parameter


def _tool_yaml(engine: dict[str, Any], safe: str, category_name: str) -> dict[str, Any]:
    fields = [
        field
        for group in engine["groups"]
        for field in group["fields"]
        if field.get("visible") is True
        and field["key"] != engine["key"]
        and field["key"] not in _RESERVED_GENERATED_FIELDS
        and field["key"] != "uule"
    ]
    parameters = [
        _field_parameter(
            field,
            str(engine["query_field"]),
            supports_uule=_supports_uule(engine),
        )
        for field in fields
    ]
    parameters.extend(
        [
            {
                "name": "extra_parameters_json",
                "type": "string",
                "required": False,
                "default": "{}",
                "label": _i18n("Extra Parameters JSON"),
                "human_description": _i18n(
                    "Advanced JSON overrides; engine and isjson remain protected."
                ),
                "llm_description": (
                    "Optional advanced JSON object override. The engine and isjson fields "
                    "remain protected."
                ),
                "form": "form",
            },
        ]
    )
    display = f"Thordata {category_name + ' ' if category_name else ''}{engine['name']}"
    return {
        "identity": {"author": "thordata", "name": safe, "label": _i18n(display)},
        "description": {
            "human": _i18n(f"Search with the fixed Thordata SERP engine {display}."),
            "llm": f"Run a Thordata SERP search using the fixed engine {engine['key']}.",
        },
        "parameters": parameters,
        "output_schema": {
            "type": "object",
            "properties": {
                "result": {
                    "type": "object",
                    "additionalProperties": True,
                }
            },
        },
        "extra": {"python": {"source": f"tools/generated/{safe}.py"}},
    }


def _class_name(safe: str) -> str:
    return "".join(part.capitalize() for part in safe.split("_")) + "Tool"


def _tool_source(engine: dict[str, Any], safe: str) -> str:
    class_name = _class_name(safe)
    dynamic = any(
        field.get("visible") is True
        and field.get("key") not in {engine["key"], "uule"}
        and field.get("control") in {"select", "multi_select"}
        for group in engine["groups"]
        for field in group["fields"]
    )
    options_method = (
        """
    def _fetch_parameter_options(self, parameter: str) -> list[ParameterOption]:
        return self.fetch_schema_options(engine=self.ENGINE_KEY, parameter=parameter)
"""
        if dynamic
        else ""
    )
    parameter_import = "from dify_plugin.entities import ParameterOption\n" if dynamic else ""
    return (
        "from collections.abc import Generator\n"
        "from typing import Any\n\n"
        + parameter_import
        + "from dify_plugin.entities.tool import ToolInvokeMessage\n\n"
        + "from tools import _base\n\n\n"
        + f"class {class_name}(_base.ThordataTool):\n"
        + f'    ENGINE_KEY = "{engine["key"]}"\n'
        + f'    QUERY_FIELD = "{engine["query_field"]}"\n\n'
        + "    def _invoke(\n"
        + "        self,\n"
        + "        tool_parameters: dict[str, Any],\n"
        + "    ) -> Generator[ToolInvokeMessage]:\n"
        + "        yield from self.invoke_fixed_engine(\n"
        + "            engine=self.ENGINE_KEY,\n"
        + "            tool_parameters=tool_parameters,\n"
        + "        )\n"
        + f"{options_method}"
    )


def _dump(value: object) -> str:
    return yaml.safe_dump(value, allow_unicode=True, sort_keys=False, default_flow_style=False)


def _render_tools(validated: dict[str, Any]) -> tuple[dict[str, str], GeneratedToolSet]:
    records = _engine_records(validated)
    rendered: dict[str, str] = {}
    for engine, safe, category_name in records:
        rendered[f"{safe}.yaml"] = _dump(_tool_yaml(engine, safe, category_name))
        rendered[f"{safe}.py"] = _tool_source(engine, safe)
    result = GeneratedToolSet(
        tool_names=tuple(engine["key"] for engine, _, _ in records),
        provider_tool_paths=tuple(f"tools/generated/{safe}.yaml" for _, safe, _ in records),
    )
    return rendered, result


def _stage_files(rendered: dict[str, str], parent: Path, prefix: str) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    try:
        for name, content in rendered.items():
            (staged / name).write_text(content, encoding="utf-8", newline="\n")
        if {path.name for path in staged.iterdir()} != set(rendered):
            raise GeneratorError("staged generated file set is incomplete")
        return staged
    except Exception:
        shutil.rmtree(staged, ignore_errors=True)
        raise


def _backup_path(path: Path, label: str) -> Path:
    return path.with_name(f".{path.name}.{label}.{next(tempfile._get_candidate_names())}")


def _replace_directory(staged: Path, output_dir: Path) -> None:
    backup = _backup_path(output_dir, "backup")
    old_moved = False
    installed = False
    try:
        if output_dir.exists():
            os.replace(output_dir, backup)
            old_moved = True
        os.replace(staged, output_dir)
        installed = True
    except Exception:
        if installed and output_dir.exists():
            shutil.rmtree(output_dir, ignore_errors=True)
        if old_moved and backup.exists():
            os.replace(backup, output_dir)
        raise
    else:
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)


def generate_tools(schema: Mapping[str, object], output_dir: Path) -> GeneratedToolSet:
    validated = _validated_schema(schema)
    rendered, result = _render_tools(validated)
    output_dir = Path(output_dir)
    staged = _stage_files(rendered, output_dir.parent, f".{output_dir.name}.staged.")
    try:
        _replace_directory(staged, output_dir)
    finally:
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)
    return result


def _provider_paths() -> list[str]:
    data = yaml.safe_load(PROVIDER_PATH.read_text(encoding="utf-8"))
    return list(data.get("tools", [])) if isinstance(data, dict) else []


def _write_generation_atomically(
    rendered: dict[str, str], output_dir: Path, provider_path: Path, provider_content: str
) -> None:
    staged_tools = _stage_files(rendered, output_dir.parent, f".{output_dir.name}.staged.")
    provider_temp = _stage_files(
        {provider_path.name: provider_content}, provider_path.parent, ".provider.staged."
    )
    tools_backup = _backup_path(output_dir, "backup")
    provider_backup = _backup_path(provider_path, "backup")
    tools_moved = False
    tools_installed = False
    provider_moved = False
    provider_installed = False
    success = False
    try:
        if output_dir.exists():
            os.replace(output_dir, tools_backup)
            tools_moved = True
        os.replace(staged_tools, output_dir)
        tools_installed = True
        if provider_path.exists():
            os.replace(provider_path, provider_backup)
            provider_moved = True
        os.replace(provider_temp / provider_path.name, provider_path)
        provider_installed = True
        success = True
    except Exception:
        if provider_installed and provider_path.exists():
            with suppress(OSError):
                provider_path.unlink(missing_ok=True)
        if provider_moved and provider_backup.exists():
            with suppress(OSError):
                os.replace(provider_backup, provider_path)
        if tools_installed and output_dir.exists():
            shutil.rmtree(output_dir, ignore_errors=True)
        if tools_moved and tools_backup.exists():
            with suppress(OSError):
                os.replace(tools_backup, output_dir)
        raise
    finally:
        if staged_tools.exists():
            shutil.rmtree(staged_tools, ignore_errors=True)
        if provider_temp.exists():
            shutil.rmtree(provider_temp, ignore_errors=True)
        if success and tools_backup.exists():
            shutil.rmtree(tools_backup, ignore_errors=True)
        if success and provider_backup.exists():
            provider_backup.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate fixed-engine Dify SERP tools.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    parser.add_argument("--snapshot", action="store_true")
    parser.add_argument("--api-key", default=None)
    args = parser.parse_args(argv)
    api_key = args.api_key if args.api_key is not None else os.environ.get("THORDATA_SERP_API_KEY")
    try:
        schema = load_generation_schema(use_snapshot=args.snapshot, api_key=api_key)
        validated = _validated_schema(schema)
        rendered, generated = _render_tools(validated)
        expected_paths = list(generated.provider_tool_paths)
        if args.check:
            with tempfile.TemporaryDirectory() as temporary:
                staged = _stage_files(rendered, Path(temporary), "generated.")
                expected_files = set(rendered)
                current_files = {
                    path.name
                    for path in GENERATED_DIR.iterdir()
                    if path.is_file() and path.suffix in {".yaml", ".py"}
                }
                if current_files != expected_files:
                    missing = sorted(expected_files - current_files)
                    extra = sorted(current_files - expected_files)
                    print(f"generated file set mismatch: missing={missing} extra={extra}")
                    return 1
                for name in sorted(expected_files):
                    current = GENERATED_DIR / name
                    expected = staged / name
                    if current.read_bytes() != expected.read_bytes():
                        print(f"generated file differs: {name}")
                        return 1
            if _provider_paths() != expected_paths:
                print("provider tool list differs from generated schema order")
                return 1
            return 0
        provider = yaml.safe_load(PROVIDER_PATH.read_text(encoding="utf-8"))
        provider["tools"] = expected_paths
        _write_generation_atomically(rendered, GENERATED_DIR, PROVIDER_PATH, _dump(provider))
        return 0
    except Exception as error:
        print(f"generation failed: {type(error).__name__}: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
