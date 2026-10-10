from collections.abc import Generator
from typing import Any

from dify_plugin.entities import ParameterOption
from dify_plugin.entities.tool import ToolInvokeMessage

from tools import _base


class BingTool(_base.ThordataTool):
    ENGINE_KEY = "bing"
    QUERY_FIELD = "q"

    def _invoke(
        self,
        tool_parameters: dict[str, Any],
    ) -> Generator[ToolInvokeMessage]:
        yield from self.invoke_fixed_engine(
            engine=self.ENGINE_KEY,
            tool_parameters=tool_parameters,
        )

    def _fetch_parameter_options(self, parameter: str) -> list[ParameterOption]:
        return self.fetch_schema_options(engine=self.ENGINE_KEY, parameter=parameter)
