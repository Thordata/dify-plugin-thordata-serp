import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
PRIVACY = ROOT / "PRIVACY.md"
REPOSITORY = "https://github.com/Thordata/dify-plugin-thordata-serp"
CONTACT = "cianna@thordata.com"
SCHEMA_ENDPOINT = "https://api.thordata.com/serp/playground/schema?lang=en"
SERP_ENDPOINT = "https://scraperapi.thordata.com/request"


def _headings(path: Path) -> set[str]:
    return {
        line.removeprefix("## ").strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("## ")
    }


def test_readme_has_all_marketplace_sections_and_public_contracts() -> None:
    document = README.read_text(encoding="utf-8")
    required_sections = {
        "Overview",
        "Features",
        "Requirements",
        "Installation",
        "Authentication",
        "Credential Validation",
        "Tools",
        "Agent Usage",
        "Workflow Usage",
        "Dynamic Schema Behavior",
        "Network Access",
        "Error Handling",
        "Maintenance",
        "Support",
        "Source",
        "Privacy",
        "License",
    }

    assert required_sections <= _headings(README)
    for value in (
        "Thordata SERP",
        "34 generated tools",
        "`extra_parameters_json`",
        "remote",
        "cache",
        "snapshot",
        SCHEMA_ENDPOINT,
        SERP_ENDPOINT,
        "api.thordata.com",
        "scraperapi.thordata.com",
        REPOSITORY,
        CONTACT,
        "credential validation may consume one successful SERP request",
        "google",
        "num=1",
    ):
        assert value in document


def test_readme_documents_schema_generated_tools_as_primary_workflow() -> None:
    document = README.read_text(encoding="utf-8")
    lowered = " ".join(document.lower().split())

    for value in (
        "34 engine-specific tools",
        "generated from the",
        "thordata schema",
        "selecting an engine",
        "generated dify tool",
        "the api is the source of truth",
        "generated yaml is checked in",
        "dify installed tool parameters are static",
        "rerun the generator",
        "package rebuild",
        "reinstall",
        "input/string",
        "number",
        "select/dynamic-select",
        "boolean/switch",
        "dynamic-select multi-select",
        "object/json",
        "google web",
        "cr",
        "lr",
        "automatic uule",
        "advanced json fallback",
        "engine and isjson",
        "dynamic-select",
        "snapshot options",
        "dify version",
        "python scripts/generate_engine_tools.py --snapshot --write",
        "python scripts/generate_engine_tools.py --check",
        "process-local environment",
        "never commit credentials",
        "never put tokens in argv",
    ):
        assert value in lowered

    assert "the search tool accepts engine, query, parameters_json" not in lowered


def test_privacy_explains_generated_tool_data_flow() -> None:
    document = " ".join(PRIVACY.read_text(encoding="utf-8").lower().split())

    for value in (
        "generated tools do not add data collection",
        "schema requests",
        "query",
        "selected engine",
        "search parameters",
        "search results",
        "sent to thordata",
        "dify manages credential storage",
        "no plugin persistence",
        "no independent logging",
    ):
        assert value in document


def test_readme_documents_local_install_and_safe_maintenance_commands() -> None:
    document = README.read_text(encoding="utf-8")

    assert ".difypkg" in document
    assert "python scripts/update_schema_snapshot.py" in document
    assert "python scripts/smoke_serp.py" in document
    assert "THORDATA_SERP_API_KEY" in document
    assert "--api-key synthetic" not in document.lower()


def test_privacy_has_required_sections_and_disclosures() -> None:
    document = PRIVACY.read_text(encoding="utf-8")
    lowered = document.lower()
    required_sections = {
        "Data Processed",
        "Purpose",
        "Transmission",
        "Credential Handling",
        "Data Retention",
        "Third-Party Service",
        "User Control",
        "Changes",
        "Contact",
    }

    assert required_sections <= _headings(PRIVACY)
    for value in (
        "api key",
        "query",
        "selected engine",
        "search parameters",
        "https",
        "dify manages credential storage",
        "does not persist",
        "does not independently log",
        "remote schema fetch",
        "no api key",
        "https://www.thordata.com",
        "https://www.thordata.com/privacy-policy",
        "https://doc.thordata.com",
        REPOSITORY.lower(),
        CONTACT,
    ):
        assert value in lowered


def test_manifest_documentation_identity_is_consistent() -> None:
    manifest = yaml.safe_load((ROOT / "manifest.yaml").read_text(encoding="utf-8"))
    readme = README.read_text(encoding="utf-8")
    privacy = PRIVACY.read_text(encoding="utf-8")

    assert manifest["privacy"] == PRIVACY.name
    assert manifest["repo"] == REPOSITORY
    assert manifest["contact"] == CONTACT
    assert REPOSITORY in readme and REPOSITORY in privacy
    assert CONTACT in readme and CONTACT in privacy


def test_release_text_is_ascii_english_and_contains_no_placeholders_or_credentials() -> None:
    paths = [
        README,
        PRIVACY,
        ROOT / "scripts" / "update_schema_snapshot.py",
        ROOT / "scripts" / "smoke_serp.py",
    ]
    forbidden = re.compile(r"\b(?:TODO|TBD|FIXME|CHANGEME|YOUR[_ -]?API[_ -]?KEY)\b", re.IGNORECASE)
    credential = re.compile(
        r"(?i)(?:bearer\s+[A-Za-z0-9._~+/=-]{16,}|"
        r"(?:api[_ -]?key|token)\s*[:=]\s*['\"][A-Za-z0-9._~+/=-]{16,}['\"])"
    )

    for path in paths:
        content = path.read_text(encoding="utf-8")
        assert content.isascii(), path
        assert not forbidden.search(content), path
        assert not credential.search(content), path
        assert not re.search(r"\b(?:zh_CN|zh_Hans|zh_TW)\b", content), path


def test_packaging_excludes_maintenance_sources_but_keeps_marketplace_docs() -> None:
    patterns = {
        line.strip()
        for line in (ROOT / ".difyignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "scripts/" in patterns
    assert "tests/" in patterns
    assert "README.md" not in patterns
    assert "PRIVACY.md" not in patterns
