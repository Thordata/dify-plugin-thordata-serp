from typing import Any

import requests
from dify_plugin import ToolProvider
from dify_plugin.errors.tool import ToolProviderCredentialValidationError

from thordata_serp.response import ResponseError, SerpApiError, safe_error_message
from thordata_serp.schema import SchemaError
from thordata_serp.serializer import ParameterError
from thordata_serp.service import build_service

_API_KEY_ERROR = "A Thordata SERP API key is required."
_UNEXPECTED_ERROR = "Unable to validate Thordata SERP credentials."


class ThordataSerpProvider(ToolProvider):
    def _validate_credentials(self, credentials: dict[str, Any]) -> None:
        api_key = credentials.get("api_key")
        if type(api_key) is not str or not api_key.strip():
            raise ToolProviderCredentialValidationError(_API_KEY_ERROR) from None
        api_key = api_key.strip()

        failure_message: str | None = None
        try:
            build_service(api_key).search(
                engine="google",
                query="Dify credential validation",
                parameters_json='{"num":1}',
                response_format="1",
                response_mode="compact",
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
            failure_message = _UNEXPECTED_ERROR

        if failure_message is not None:
            raise ToolProviderCredentialValidationError(failure_message) from None
