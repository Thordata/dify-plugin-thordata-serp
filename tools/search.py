from collections.abc import Generator
from typing import Any

from dify_plugin.entities.tool import ToolInvokeMessage

from . import _base


class SearchTool(_base.ThordataTool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        yield from self.invoke_service(
            lambda service: service.search(
                engine=tool_parameters.get("engine", ""),
                query=tool_parameters.get("query", ""),
                parameters_json=tool_parameters.get("parameters_json", "{}"),
                response_format=tool_parameters.get("response_format", "1"),
                response_mode=tool_parameters.get("response_mode", "compact"),
            )
        )
