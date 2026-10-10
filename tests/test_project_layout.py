from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PLUGIN_ROOT.parent


def test_plugin_is_nested_under_dify_workspace() -> None:
    assert PLUGIN_ROOT.name == "thordata-serp-plugin"
    assert (PLUGIN_ROOT / "manifest.yaml").is_file()
    assert (WORKSPACE_ROOT / "tutorial").is_dir()
    assert not (PLUGIN_ROOT / "tutorial").exists()


def test_workspace_content_is_not_registered_as_plugin_runtime() -> None:
    difyignore = (PLUGIN_ROOT / ".difyignore").read_text(encoding="utf-8")

    assert "tests/" in difyignore
    assert "scripts/" in difyignore
    assert not (PLUGIN_ROOT / "_dify_tut_work").exists()
