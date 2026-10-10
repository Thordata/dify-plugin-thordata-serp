import importlib.util
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from dify_plugin.core.plugin_registration import PluginRegistration
from dify_plugin.errors.tool import ToolProviderCredentialValidationError

ROOT = Path(__file__).resolve().parents[1]


def load_yaml(relative: str) -> dict:
    return yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))


def load_provider_class() -> type[Any]:
    source = ROOT / "provider" / "thordata_serp.py"
    spec = importlib.util.spec_from_file_location("thordata_serp_provider", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ThordataSerpProvider


def test_manifest_identity_and_marketplace_metadata() -> None:
    manifest = load_yaml("manifest.yaml")

    assert manifest["version"] == "0.1.0"
    assert manifest["type"] == "plugin"
    assert manifest["author"] == "thordata"
    assert manifest["author"].islower()
    assert manifest["name"] == "thordata_serp"
    assert manifest["repo"] == "https://github.com/Thordata/dify-plugin-thordata-serp"
    assert manifest["contact"] == "cianna@thordata.com"
    assert manifest["privacy"] == "PRIVACY.md"
    assert manifest["plugins"]["tools"] == ["provider/thordata_serp.yaml"]
    assert manifest["meta"]["runner"] == {
        "language": "python",
        "version": "3.12",
        "entrypoint": "main",
    }
    assert manifest["network"]["domains"] == [
        "api.thordata.com",
        "scraperapi.thordata.com",
    ]


def test_provider_declares_one_secret_and_all_generated_tools() -> None:
    provider = load_yaml("provider/thordata_serp.yaml")

    assert provider["identity"]["author"] == "thordata"
    assert provider["identity"]["author"].islower()
    assert provider["identity"]["name"] == "thordata_serp"
    assert list(provider["credentials_for_provider"]) == ["api_key"]
    assert provider["credentials_for_provider"]["api_key"]["type"] == "secret-input"
    assert provider["credentials_for_provider"]["api_key"]["required"] is True
    generated = list((ROOT / "tools" / "generated").glob("*.yaml"))
    assert len(generated) == 34
    assert len(provider["tools"]) == 34
    assert set(provider["tools"]) == {f"tools/generated/{path.stem}.yaml" for path in generated}


OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "result": {
            "type": "object",
            "additionalProperties": True,
        }
    },
}

TOOL_CONTRACTS = {
    "list_engines": {
        "identity": {
            "author": "Thordata",
            "name": "list_engines",
            "label": {"en_US": "List Search Engines"},
        },
        "description": {
            "human": {"en_US": "List the available Thordata SERP search engines."},
            "llm": (
                "List available Thordata SERP engines and their identifiers before "
                "choosing an engine for a search."
            ),
        },
        "parameters": [],
        "extra": {"python": {"source": "tools/list_engines.py"}},
    },
    "get_engine_schema": {
        "identity": {
            "author": "Thordata",
            "name": "get_engine_schema",
            "label": {"en_US": "Get Engine Schema"},
        },
        "description": {
            "human": {"en_US": "Get the supported parameters for a Thordata SERP search engine."},
            "llm": (
                "Get the parameter schema for one Thordata SERP engine before building "
                "an advanced search request."
            ),
        },
        "parameters": [
            {
                "name": "engine",
                "type": "string",
                "required": True,
                "label": {"en_US": "Engine"},
                "human_description": {"en_US": "The engine identifier returned by list_engines."},
                "llm_description": "Exact engine identifier returned by list_engines.",
                "form": "llm",
            }
        ],
        "extra": {"python": {"source": "tools/get_engine_schema.py"}},
    },
    "search": {
        "identity": {
            "author": "Thordata",
            "name": "search",
            "label": {"en_US": "Search"},
        },
        "description": {
            "human": {"en_US": "Run a Thordata SERP search and return structured results."},
            "llm": (
                "Search through the Thordata SERP API. Use list_engines and "
                "get_engine_schema before advanced searches to select a valid engine "
                "and parameters."
            ),
        },
        "parameters": [
            {
                "name": "engine",
                "type": "string",
                "required": False,
                "default": "",
                "label": {"en_US": "Engine"},
                "human_description": {
                    "en_US": "The search engine identifier; leave blank to use the default."
                },
                "llm_description": (
                    "Engine identifier from list_engines, or an empty string for the default."
                ),
                "form": "llm",
            },
            {
                "name": "query",
                "type": "string",
                "required": False,
                "default": "",
                "label": {"en_US": "Query"},
                "human_description": {"en_US": "The search query text."},
                "llm_description": "Search query text accepted by the selected engine.",
                "form": "llm",
            },
            {
                "name": "parameters_json",
                "type": "string",
                "required": False,
                "default": "{}",
                "label": {"en_US": "Parameters JSON"},
                "human_description": {
                    "en_US": "Additional engine parameters as a JSON object string."
                },
                "llm_description": (
                    "JSON object string containing parameters supported by get_engine_schema."
                ),
                "form": "llm",
            },
            {
                "name": "response_format",
                "type": "select",
                "required": True,
                "default": "1",
                "label": {"en_US": "Response Format"},
                "human_description": {"en_US": "The response representation to request."},
                "form": "form",
                "options": [
                    {"value": "1", "label": {"en_US": "Structured JSON"}},
                    {"value": "2", "label": {"en_US": "JSON and HTML"}},
                    {"value": "3", "label": {"en_US": "HTML"}},
                ],
            },
            {
                "name": "response_mode",
                "type": "select",
                "required": True,
                "default": "compact",
                "label": {"en_US": "Response Mode"},
                "human_description": {"en_US": "Return compact results or the complete response."},
                "form": "form",
                "options": [
                    {"value": "compact", "label": {"en_US": "Compact"}},
                    {"value": "complete", "label": {"en_US": "Complete"}},
                ],
            },
        ],
        "extra": {"python": {"source": "tools/search.py"}},
    },
}


@pytest.mark.parametrize("tool_name", ["list_engines", "get_engine_schema", "search"])
def test_tool_yaml_matches_exact_contract(tool_name: str) -> None:
    declaration = load_yaml(f"tools/{tool_name}.yaml")

    assert declaration == {
        **TOOL_CONTRACTS[tool_name],
        "output_schema": OUTPUT_SCHEMA,
    }


@pytest.mark.parametrize("tool_name", ["list_engines", "get_engine_schema", "search"])
def test_tool_sources_exist_and_localization_is_english_only(tool_name: str) -> None:
    declaration = load_yaml(f"tools/{tool_name}.yaml")
    source = declaration["extra"]["python"]["source"]

    assert source == f"tools/{tool_name}.py"
    assert (ROOT / source).is_file()
    assert set(declaration["identity"]["label"]) == {"en_US"}
    assert set(declaration["description"]["human"]) == {"en_US"}
    for parameter in declaration["parameters"]:
        assert set(parameter["label"]) == {"en_US"}
        assert set(parameter["human_description"]) == {"en_US"}
        for option in parameter.get("options", []):
            assert set(option["label"]) == {"en_US"}


def test_full_plugin_registration_loads_provider_and_all_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(ROOT)

    registration = PluginRegistration(None)  # type: ignore[arg-type]
    provider, provider_class, tools = registration.tools_mapping["thordata_serp"]

    assert provider.identity.name == "thordata_serp"
    assert provider_class.__name__ == "ThordataSerpProvider"
    provider_data = load_yaml("provider/thordata_serp.yaml")
    expected_names = [Path(path).stem for path in provider_data["tools"]]
    assert list(tools) == expected_names
    assert len(tools) == 34
    assert all(tool_class.__name__.endswith("Tool") for _, tool_class in tools.values())


def test_all_declared_assets_and_sources_exist() -> None:
    manifest = load_yaml("manifest.yaml")
    provider = load_yaml("provider/thordata_serp.yaml")

    for asset in (
        manifest["icon"],
        manifest["icon_dark"],
        provider["identity"]["icon"],
        provider["identity"]["icon_dark"],
    ):
        assert (ROOT / "_assets" / asset).is_file()
    assert (ROOT / provider["extra"]["python"]["source"]).is_file()


@pytest.mark.parametrize("credentials", [{}, {"api_key": 123}, {"api_key": "   "}])
def test_provider_rejects_blank_api_keys(credentials: dict[str, object]) -> None:
    provider_class = load_provider_class()
    provider = object.__new__(provider_class)

    with pytest.raises(ToolProviderCredentialValidationError) as captured:
        provider._validate_credentials(credentials)

    assert str(captured.value) == "A Thordata SERP API key is required."


def test_python_and_ruff_configuration_is_bounded() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["requires-python"] == ">=3.12,<3.13"
    assert project["project"]["dependencies"] == [
        "dify-plugin>=0.10.2,<0.11.0",
        "requests>=2.32,<3.0",
    ]
    assert project["tool"]["ruff"] == {
        "line-length": 100,
        "target-version": "py312",
        "lint": {"select": ["E", "F", "I", "UP", "B", "SIM"]},
    }


def test_license_and_icons_match_approved_sources() -> None:
    repository_root = ROOT.parent.parent

    assert (ROOT / "LICENSE").read_text(encoding="utf-8").splitlines() == (
        repository_root / "llama" / "LICENSE"
    ).read_text(encoding="utf-8").splitlines()
    assert (ROOT / "_assets" / "icon.svg").read_bytes() == (
        repository_root / "n8n" / "n8n-nodes-thordata" / "nodes" / "Thordata" / "thordata.svg"
    ).read_bytes()
    assert (ROOT / "_assets" / "icon-dark.svg").read_bytes() == (
        repository_root / "n8n" / "n8n-nodes-thordata" / "nodes" / "Thordata" / "thordata.dark.svg"
    ).read_bytes()


def test_dify_package_excludes_development_and_generated_artifacts() -> None:
    exclusions = set((ROOT / ".difyignore").read_text(encoding="utf-8").splitlines())

    assert {
        ".env*",
        ".venv/",
        ".git/",
        ".gitignore",
        ".github/",
        ".idea/",
        ".vscode/",
        ".DS_Store",
        "Thumbs.db",
        "tests/",
        "scripts/",
        "**/__pycache__/",
        "*.py[cod]",
        ".pytest_cache/",
        ".ruff_cache/",
        ".mypy_cache/",
        ".hypothesis/",
        ".coverage",
        ".coverage.*",
        "coverage.xml",
        "htmlcov/",
        "logs/",
        "*.log",
        "build/",
        "dist/",
        "*.egg-info/",
        "*.difypkg",
    } <= exclusions
    assert "pyproject.toml" not in exclusions
    assert "uv.lock" not in exclusions
