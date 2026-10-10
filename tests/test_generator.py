from __future__ import annotations

import importlib.util
import os
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from dify_plugin.core.plugin_registration import PluginRegistration
from dify_plugin.entities import I18nObject, ParameterOption

from scripts.generate_engine_tools import (
    GeneratorError,
    generate_provider_tool_paths,
    generate_tools,
    load_generation_schema,
    main,
)
from thordata_serp.schema import iter_engines

ROOT = Path(__file__).resolve().parents[1]


def test_snapshot_generation_is_deterministic_and_covers_all_engines(tmp_path: Path) -> None:
    schema = load_generation_schema(use_snapshot=True)
    result = generate_tools(schema, tmp_path)

    engines = list(iter_engines(schema))
    assert len(engines) == 34
    assert result.tool_names == tuple(engine["key"] for engine in engines)
    assert result.provider_tool_paths == tuple(
        f"tools/generated/{engine['key']}.yaml" for engine in engines
    )
    assert len(list(tmp_path.glob("*.yaml"))) == 34
    assert len(list(tmp_path.glob("*.py"))) == 34

    google = yaml.safe_load((tmp_path / "google.yaml").read_text(encoding="utf-8"))
    assert google["identity"] == {
        "author": "thordata",
        "name": "google",
        "label": {"en_US": "Thordata Google Search"},
    }
    names = [parameter["name"] for parameter in google["parameters"]]
    assert "uule" not in names
    assert "json" not in names
    assert "isjson" not in names
    assert "extra_parameters_json" in names
    assert "response_format" not in names
    assert "response_mode" not in names
    google_web = yaml.safe_load((tmp_path / "google_web.yaml").read_text(encoding="utf-8"))
    parameters = {parameter["name"]: parameter for parameter in google_web["parameters"]}
    assert "uule" not in parameters
    assert "location" in parameters
    assert "UULE:" not in parameters["location"]["options"][1]["label"]["en_US"]
    assert "derived automatically" in parameters["location"]["human_description"]["en_US"]
    for name in ("cr", "lr"):
        assert parameters[name]["type"] == "dynamic-select"
        assert parameters[name]["multiple"] is True
        assert parameters[name]["default"] == []
        assert parameters[name]["options"]
    location = next(
        parameter for parameter in google_web["parameters"] if parameter["name"] == "location"
    )
    assert "UULE:" not in location["options"][1]["label"]["en_US"]

    google_ai_mode = yaml.safe_load((tmp_path / "google_ai_mode.yaml").read_text(encoding="utf-8"))
    ai_parameters = {parameter["name"]: parameter for parameter in google_ai_mode["parameters"]}
    assert "uule" not in ai_parameters
    assert "location" in ai_parameters

    bing = yaml.safe_load((tmp_path / "bing.yaml").read_text(encoding="utf-8"))
    bing_location = next(
        parameter for parameter in bing["parameters"] if parameter["name"] == "location"
    )
    assert "UULE:" not in bing_location["options"][1]["label"]["en_US"]
    assert "UULE" not in bing_location["human_description"]["en_US"]
    assert google["output_schema"] == {
        "type": "object",
        "properties": {
            "result": {
                "type": "object",
                "additionalProperties": True,
            }
        },
    }


def test_generated_source_has_one_loader_class_and_fixed_engine(tmp_path: Path) -> None:
    schema = load_generation_schema(use_snapshot=True)
    generate_tools(schema, tmp_path)

    source = (tmp_path / "google.py").read_text(encoding="utf-8")
    assert 'ENGINE_KEY = "google"' in source
    assert (
        "yield from self.invoke_fixed_engine(\n"
        "            engine=self.ENGINE_KEY,\n"
        "            tool_parameters=tool_parameters,\n"
        "        )" in source
    )

    spec = importlib.util.spec_from_file_location("generated_google", tmp_path / "google.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    concrete = [
        value
        for value in vars(module).values()
        if isinstance(value, type) and value.__name__.endswith("Tool")
    ]
    assert [value.__name__ for value in concrete] == ["GoogleTool"]
    assert module.GoogleTool.ENGINE_KEY == "google"


def test_generated_multi_select_options_are_dynamic_and_location_labels_are_plain(
    tmp_path: Path,
) -> None:
    schema = load_generation_schema(use_snapshot=True)
    generate_tools(schema, tmp_path)
    payload = yaml.safe_load((tmp_path / "google_web.yaml").read_text(encoding="utf-8"))
    parameters = {parameter["name"]: parameter for parameter in payload["parameters"]}
    assert parameters["cr"]["type"] == "dynamic-select"
    assert parameters["cr"]["multiple"] is True
    assert parameters["cr"]["default"] == []
    assert parameters["lr"]["type"] == "dynamic-select"
    assert parameters["lr"]["multiple"] is True
    assert parameters["lr"]["default"] == []
    location_options = parameters["location"]["options"]
    assert all("UULE:" not in option["label"]["en_US"] for option in location_options)
    assert "uule" not in parameters


def test_provider_paths_follow_schema_order() -> None:
    schema = load_generation_schema(use_snapshot=True)
    engines = list(iter_engines(schema))
    assert generate_provider_tool_paths(schema) == [
        f"tools/generated/{engine['key']}.yaml" for engine in engines
    ]


def test_generator_rejects_python_unsafe_engine_and_field_keys_without_writing(
    tmp_path: Path,
) -> None:
    schema = deepcopy(load_generation_schema(use_snapshot=True))
    engine = schema["categories"][0]["engines"][0]
    engine["key"] = 'x";raise RuntimeError("pwned")#\n'
    before = sorted(tmp_path.iterdir())

    with pytest.raises(GeneratorError, match="safe identifier"):
        generate_tools(schema, tmp_path)

    assert sorted(tmp_path.iterdir()) == before


def test_generator_rejects_reserved_generated_parameter_name() -> None:
    schema = deepcopy(load_generation_schema(use_snapshot=True))
    field = schema["categories"][0]["engines"][0]["groups"][0]["fields"][1]
    field["key"] = "response_format"

    with pytest.raises(GeneratorError, match="conflicts"):
        generate_provider_tool_paths(schema)


def test_generator_rejects_non_string_multi_select_values() -> None:
    schema = deepcopy(load_generation_schema(use_snapshot=True))
    field = next(
        field
        for group in schema["categories"][0]["engines"][0]["groups"]
        for field in group["fields"]
        if field["control"] == "multi_select"
    )
    field["options"][0]["value"] = 1

    with pytest.raises(GeneratorError, match="non-string values"):
        generate_provider_tool_paths(schema)


def test_every_generated_wrapper_matches_schema_and_dynamic_hook_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = load_generation_schema(use_snapshot=True)
    monkeypatch.chdir(ROOT)
    registration = PluginRegistration(None)  # type: ignore[arg-type]
    _, _, tools = registration.tools_mapping["thordata_serp"]
    engines = {engine["key"]: engine for engine in iter_engines(schema)}

    assert len(tools) == 34
    for name, (declaration, tool_class) in tools.items():
        engine = engines[name]
        assert declaration.extra.python.source == f"tools/generated/{name}.py"
        assert engine["key"] == tool_class.ENGINE_KEY
        assert engine["query_field"] == tool_class.QUERY_FIELD
        dynamic_parameters = [
            parameter.name
            for parameter in declaration.parameters
            if parameter.type.value == "dynamic-select"
        ]
        if dynamic_parameters:
            seen: list[object] = []

            def fetch(
                _instance: object,
                *,
                engine: str,
                parameter: str,
                _seen: list[object] = seen,
            ) -> list[ParameterOption]:
                assert isinstance(parameter, str)
                _seen.append((engine, parameter))
                return [ParameterOption(value="x", label=I18nObject(en_US="X"))]

            monkeypatch.setattr(tool_class, "fetch_schema_options", fetch)
            instance = object.__new__(tool_class)
            options = instance._fetch_parameter_options(dynamic_parameters[0])
            assert isinstance(options[0], ParameterOption)
            assert seen == [(engine["key"], dynamic_parameters[0])]


def test_check_rejects_stale_generated_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    schema = load_generation_schema(use_snapshot=True)
    generated_dir = tmp_path / "generated"
    provider_path = tmp_path / "provider.yaml"
    generate_tools(schema, generated_dir)
    provider = yaml.safe_load(
        (ROOT / "provider" / "thordata_serp.yaml").read_text(encoding="utf-8")
    )
    provider_path.write_text(yaml.safe_dump(provider, sort_keys=False), encoding="utf-8")
    (generated_dir / "stale.py").write_text("stale\n", encoding="utf-8")
    monkeypatch.setattr("scripts.generate_engine_tools.GENERATED_DIR", generated_dir)
    monkeypatch.setattr("scripts.generate_engine_tools.PROVIDER_PATH", provider_path)

    assert main(["--check", "--snapshot"]) == 1


def test_generation_failure_preserves_existing_generated_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    schema = load_generation_schema(use_snapshot=True)
    output_dir = tmp_path / "generated"
    output_dir.mkdir()
    (output_dir / "google.yaml").write_text("old yaml\n", encoding="utf-8")
    (output_dir / "google.py").write_text("old py\n", encoding="utf-8")
    before = {path.name: path.read_bytes() for path in output_dir.iterdir()}
    replace = os.replace
    calls = 0

    def fail_after_first(source: object, target: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected replace failure")
        replace(source, target)

    monkeypatch.setattr("scripts.generate_engine_tools.os.replace", fail_after_first)
    with pytest.raises(OSError, match="injected replace failure"):
        generate_tools(schema, output_dir)

    assert {path.name: path.read_bytes() for path in output_dir.iterdir()} == before


def test_first_directory_replace_failure_preserves_existing_generated_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    schema = load_generation_schema(use_snapshot=True)
    output_dir = tmp_path / "generated"
    output_dir.mkdir()
    (output_dir / "sentinel.txt").write_text("old\n", encoding="utf-8")
    before = {path.name: path.read_bytes() for path in output_dir.iterdir()}

    def fail_first_replace(_source: object, _target: object) -> None:
        raise OSError("injected first replace failure")

    monkeypatch.setattr("scripts.generate_engine_tools.os.replace", fail_first_replace)
    with pytest.raises(OSError, match="injected first replace failure"):
        generate_tools(schema, output_dir)

    assert {path.name: path.read_bytes() for path in output_dir.iterdir()} == before


def test_restore_failure_leaves_directory_backup_for_recovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    schema = load_generation_schema(use_snapshot=True)
    output_dir = tmp_path / "generated"
    output_dir.mkdir()
    (output_dir / "sentinel.txt").write_text("old\n", encoding="utf-8")
    replace = os.replace
    calls = 0

    def fail_swap_and_restore(source: object, target: object) -> None:
        nonlocal calls
        calls += 1
        if calls in {2, 3}:
            raise OSError("injected restore failure")
        replace(source, target)

    monkeypatch.setattr("scripts.generate_engine_tools.os.replace", fail_swap_and_restore)
    with pytest.raises(OSError, match="injected restore failure"):
        generate_tools(schema, output_dir)

    assert list(tmp_path.glob(".generated.backup.*"))


def test_first_write_replace_failure_preserves_output_and_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    generated_dir = tmp_path / "generated"
    provider_path = tmp_path / "provider.yaml"
    generated_dir.mkdir()
    (generated_dir / "sentinel.txt").write_text("old\n", encoding="utf-8")
    provider_path.write_text("tools: [old]\n", encoding="utf-8")
    before_generated = {path.name: path.read_bytes() for path in generated_dir.iterdir()}
    before_provider = provider_path.read_bytes()
    monkeypatch.setattr("scripts.generate_engine_tools.GENERATED_DIR", generated_dir)
    monkeypatch.setattr("scripts.generate_engine_tools.PROVIDER_PATH", provider_path)

    def fail_first_replace(_source: object, _target: object) -> None:
        raise OSError("injected first replace failure")

    monkeypatch.setattr("scripts.generate_engine_tools.os.replace", fail_first_replace)
    assert main(["--write", "--snapshot"]) == 1

    assert {path.name: path.read_bytes() for path in generated_dir.iterdir()} == before_generated
    assert provider_path.read_bytes() == before_provider


def test_generator_rejects_unsafe_ordinary_field_name() -> None:
    schema = deepcopy(load_generation_schema(use_snapshot=True))
    field = schema["categories"][0]["engines"][0]["groups"][0]["fields"][1]
    field["key"] = 'bad"field\n'

    with pytest.raises(GeneratorError, match="parameter name"):
        generate_provider_tool_paths(schema)


def test_write_failure_preserves_generated_directory_and_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    generated_dir = tmp_path / "generated"
    provider_path = tmp_path / "provider.yaml"
    source_generated = ROOT / "tools" / "generated"
    generated_dir.mkdir()
    for source in (source_generated / "google.yaml", source_generated / "google.py"):
        (generated_dir / source.name).write_bytes(source.read_bytes())
    provider_path.write_bytes((ROOT / "provider" / "thordata_serp.yaml").read_bytes())
    before_generated = {path.name: path.read_bytes() for path in generated_dir.iterdir()}
    before_provider = provider_path.read_bytes()
    monkeypatch.setattr("scripts.generate_engine_tools.GENERATED_DIR", generated_dir)
    monkeypatch.setattr("scripts.generate_engine_tools.PROVIDER_PATH", provider_path)
    replace = os.replace
    calls = 0

    def fail_provider_replace(source: object, target: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError("injected provider replace failure")
        replace(source, target)

    monkeypatch.setattr("scripts.generate_engine_tools.os.replace", fail_provider_replace)
    assert main(["--write", "--snapshot"]) == 1

    assert {path.name: path.read_bytes() for path in generated_dir.iterdir()} == before_generated
    assert provider_path.read_bytes() == before_provider
