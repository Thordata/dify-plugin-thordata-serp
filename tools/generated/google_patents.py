from collections.abc import Generator
from typing import Any

from dify_plugin.entities.tool import ToolInvokeMessage

from tools import _base


class GooglePatentsTool(_base.ThordataTool):
    ENGINE_KEY = "google_patents"
    QUERY_FIELD = "q"

    def _invoke(
        self,
        tool_parameters: dict[str, Any],
    ) -> Generator[ToolInvokeMessage]:
        yield from self.invoke_fixed_engine(
            engine=self.ENGINE_KEY,
            tool_parameters=tool_parameters,
        )
