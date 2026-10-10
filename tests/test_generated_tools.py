from __future__ import annotations

import inspect

from dify_plugin.core.plugin_registration import PluginRegistration
from dify_plugin.entities import ParameterOption

from tools.generated.google_web import GoogleWebTool


def test_generated_dynamic_hook_uses_sdk_parameter_option_annotation() -> None:
    annotation = inspect.signature(GoogleWebTool._fetch_parameter_options).return_annotation
    assert annotation == "list[ParameterOption]" or annotation == list[ParameterOption]


def test_generated_multi_select_uses_dynamic_select_sdk_contract() -> None:
    registration = PluginRegistration(None)  # type: ignore[arg-type]
    _, _, tools = registration.tools_mapping["thordata_serp"]
    declaration, _ = tools["google_web"]
    parameters = {parameter.name: parameter for parameter in declaration.parameters}

    assert "uule" not in parameters
    assert "location" in parameters
    assert "derived automatically" in parameters["location"].human_description.en_us

    assert parameters["cr"].type.value == "dynamic-select"
    assert parameters["cr"].multiple is True
    assert parameters["cr"].default == []
