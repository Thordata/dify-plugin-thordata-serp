from collections.abc import Generator
from typing import Any

from dify_plugin.entities.tool import ToolInvokeMessage

from . import _base


class ListEnginesTool(_base.ThordataTool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        yield from self.invoke_service(lambda service: service.list_engines())
