from collections.abc import Callable, Generator
from typing import Any

import requests
from dify_plugin import Tool
from dify_plugin.entities import I18nObject, ParameterOption
from dify_plugin.entities.tool import ToolInvokeMessage

from thordata_serp.response import ResponseError, SerpApiError, safe_error_message
from thordata_serp.schema import SchemaError
from thordata_serp.serializer import ParameterError
from thordata_serp.service import ThordataSerpService, build_service

_UNEXPECTED_TOOL_ERROR = "Unexpected Thordata SERP tool failure."


# dify-plugin 0.10.2 provides provider credential errors, but no tool invocation error.
class ToolInvokeError(RuntimeError):
    pass


class ThordataTool(Tool):
    def invoke_fixed_engine(
        self,
        *,
        engine: str,
        tool_parameters: dict[str, Any],
    ) -> Generator[ToolInvokeMessage]:
        parameters = dict(tool_parameters)
        parameters.pop("engine", None)
        query_field = getattr(self, "QUERY_FIELD", "")
        query = parameters.pop(query_field, "")
        extra_parameters_json = parameters.pop("extra_parameters_json", "{}")
        response_format = parameters.pop("response_format", "1")
        response_mode = parameters.pop("response_mode", "compact")
        yield from self.invoke_service(
            lambda service: service.search_fixed_engine(
                engine=engine,
                query=query,
                parameters=parameters,
                extra_parameters_json=extra_parameters_json,
                response_format=response_format,
                response_mode=response_mode,
            )
        )

    def fetch_schema_options(
        self,
        *,
        engine: str,
        parameter: str | dict[str, Any],
    ) -> list[ParameterOption]:
        api_key = self.runtime.credentials.get("api_key")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ToolInvokeError("A Thordata SERP API key is required.") from None
        key = parameter if isinstance(parameter, str) else parameter.get("name")
        if not isinstance(key, str) or not key.strip():
            return []
        failure_message: str | None = None
        options: list[ParameterOption] = []
        try:
            result = build_service(api_key.strip()).get_engine_schema(engine)
            for group in result["engine"]["groups"]:
                for field in group["fields"]:
                    if field.get("key") == key:
                        if field.get("visible") is False or field.get("control") not in {
                            "select",
                            "multi_select",
                        }:
                            break
                        options = []
                        for option in field.get("options", []):
                            value = str(option["value"])
                            label = str(option["label"])
                            options.append(
                                ParameterOption(
                                    value=value,
                                    label=I18nObject(en_US=label),
                                )
                            )
                        break
        except (
            SchemaError,
            ParameterError,
            ResponseError,
            SerpApiError,
            requests.RequestException,
        ) as error:
            failure_message = safe_error_message(error, api_key.strip())
        except Exception:
            failure_message = _UNEXPECTED_TOOL_ERROR

        if failure_message is not None:
            raise ToolInvokeError(failure_message) from None
        return options

    def invoke_service(
        self,
        operation: Callable[[ThordataSerpService], dict[str, Any]],
    ) -> Generator[ToolInvokeMessage]:
        api_key = self.runtime.credentials.get("api_key")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ToolInvokeError("A Thordata SERP API key is required.") from None
        api_key = api_key.strip()

        failure_message: str | None = None
        messages: tuple[ToolInvokeMessage, ToolInvokeMessage] = ()
        try:
            result = operation(build_service(api_key))
            messages = (
                self.create_json_message(json=result),
                self.create_variable_message(variable_name="result", variable_value=result),
            )
        except (
            SchemaError,
            ParameterError,
            ResponseError,
            SerpApiError,
            requests.RequestException,
        ) as error:
            failure_message = safe_error_message(error, api_key)
        except Exception:
            failure_message = _UNEXPECTED_TOOL_ERROR

        if failure_message is not None:
            raise ToolInvokeError(failure_message) from None

        yield from messages
