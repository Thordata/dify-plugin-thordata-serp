"""Bounded HTTP clients for Thordata schema and SERP requests."""

import json
import math
import unicodedata
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote_plus, urlparse

import requests

from .json_safety import validate_json_tree
from .response import ResponseError

DEFAULT_SCHEMA_ENDPOINT = "https://api.thordata.com/serp/playground/schema?lang=en"
DEFAULT_SERP_ENDPOINT = "https://scraperapi.thordata.com/request"
MAX_REQUEST_SIZE = 1 << 20
MAX_RESPONSE_SIZE = 10 << 20
_STREAM_CHUNK_SIZE = 64 * 1024
_ENDPOINT_ERROR = "endpoint must be a nonblank http or https URL"


class RequestEncodingError(ValueError):
    """Raised when a SERP request form cannot be encoded safely."""


def _validated_endpoint(value: Any) -> str:
    if type(value) is not str:
        raise ValueError(_ENDPOINT_ERROR)
    endpoint = str.strip(value)
    scheme_separator = endpoint.find("://")
    if scheme_separator >= 0:
        authority_start = scheme_separator + 3
        authority_end = len(endpoint)
        for delimiter in "/?#":
            position = endpoint.find(delimiter, authority_start)
            if position >= 0:
                authority_end = min(authority_end, position)
        authority = endpoint[authority_start:authority_end]
        if any(
            character.isspace() or unicodedata.category(character) == "Cc"
            for character in authority
        ):
            raise ValueError(_ENDPOINT_ERROR)
    try:
        parsed = urlparse(endpoint)
        hostname = parsed.hostname
        _ = parsed.port
        username = parsed.username
        password = parsed.password
    except ValueError:
        raise ValueError(_ENDPOINT_ERROR) from None
    if (
        parsed.scheme not in {"http", "https"}
        or hostname is None
        or not hostname.strip()
        or username is not None
        or password is not None
    ):
        raise ValueError(_ENDPOINT_ERROR)
    return endpoint


def _validated_timeout(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("timeout must be a positive integer")
    return value


def _read_limited(response: requests.Response) -> bytes:
    content_length = response.headers.get("Content-Length")
    try:
        if content_length is not None and int(content_length) > MAX_RESPONSE_SIZE:
            raise ResponseError("response exceeds maximum size")
    except (TypeError, ValueError, OverflowError):
        pass

    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=_STREAM_CHUNK_SIZE):
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_RESPONSE_SIZE:
            raise ResponseError("response exceeds maximum size")
        chunks.append(chunk)
    return b"".join(chunks)


def _form_component(value: Any) -> str | bytes:
    value_type = type(value)
    if value_type is str or value_type is bytes:
        return value
    if value_type is bool:
        return "True" if value else "False"
    if value_type is int:
        bit_length = int.bit_length(value)
        minimum_digits = ((bit_length - 1) * 301_029) // 1_000_000 + 1
        if minimum_digits > MAX_REQUEST_SIZE:
            raise RequestEncodingError("SERP request exceeds maximum size")
        try:
            return int.__str__(value)
        except (OverflowError, ValueError) as error:
            raise RequestEncodingError(
                "SERP request component cannot be converted to text"
            ) from error
    if value_type is float:
        if not math.isfinite(value):
            raise RequestEncodingError("SERP request float components must be finite")
        return float.__str__(value)
    raise RequestEncodingError("SERP request form components must be built-in scalar values")


def _quoted_component(value: str | bytes, remaining_budget: int) -> tuple[str, int]:
    try:
        encoded = quote_plus(value)
        encoded_size = len(str.encode(encoded, "utf-8"))
    except (TypeError, ValueError) as error:
        raise RequestEncodingError("SERP request contains invalid encoded text") from error
    if encoded_size > remaining_budget:
        raise RequestEncodingError("SERP request exceeds maximum size")
    return encoded, encoded_size


def _escaped_ascii_size(value: int) -> int:
    if (
        48 <= value <= 57
        or 65 <= value <= 90
        or 97 <= value <= 122
        or value in (32, 45, 46, 95, 126)
    ):
        return 1
    return 3


def _utf8_size(value: int) -> int:
    if 0xD800 <= value <= 0xDFFF:
        raise RequestEncodingError("SERP request contains invalid Unicode")
    if value <= 0x7F:
        return 1
    if value <= 0x7FF:
        return 2
    if value <= 0xFFFF:
        return 3
    return 4


def _encoded_component_size(value: str | bytes, remaining_budget: int) -> int:
    size = 0
    if isinstance(value, bytes):
        for byte in value:
            increment = _escaped_ascii_size(byte)
            if increment > remaining_budget - size:
                return remaining_budget + 1
            size += increment
        return size

    for character in value:
        code_point = ord(character)
        increment = (
            _escaped_ascii_size(code_point) if code_point <= 0x7F else _utf8_size(code_point) * 3
        )
        if increment > remaining_budget - size:
            return remaining_budget + 1
        size += increment
    return size


def _encode_form(form: Mapping[Any, Any]) -> str:
    """Encode a form only after proving its encoded body is within the limit."""
    if not isinstance(form, Mapping):
        raise RequestEncodingError("SERP request form must be a mapping")

    parts: list[str] = []
    encoded_size = 0
    for key, value in form.items():
        encoded_key = _form_component(key)
        encoded_value = _form_component(value)
        separator_size = 1 if parts else 0
        remaining_budget = MAX_REQUEST_SIZE - encoded_size - separator_size - 1
        if remaining_budget < 0:
            raise RequestEncodingError("SERP request exceeds maximum size")

        key_size = _encoded_component_size(encoded_key, remaining_budget)
        if key_size > remaining_budget:
            raise RequestEncodingError("SERP request exceeds maximum size")
        value_budget = remaining_budget - key_size
        value_size = _encoded_component_size(encoded_value, value_budget)
        if value_size > value_budget:
            raise RequestEncodingError("SERP request exceeds maximum size")

        quoted_key, actual_key_size = _quoted_component(encoded_key, remaining_budget)
        quoted_value, actual_value_size = _quoted_component(
            encoded_value, remaining_budget - actual_key_size
        )
        parts.append(f"{quoted_key}={quoted_value}")
        encoded_size += separator_size + actual_key_size + 1 + actual_value_size

    body = "&".join(parts)
    try:
        body_size = len(str.encode(body, "utf-8"))
    except UnicodeEncodeError as error:
        raise RequestEncodingError("SERP request contains invalid encoded text") from error
    if body_size > MAX_REQUEST_SIZE:
        raise RequestEncodingError("SERP request exceeds maximum size")
    return body


class SchemaHttpClient:
    """Fetch JSON schema documents from the fixed Thordata schema endpoint."""

    def __init__(
        self,
        timeout: int = 30,
        *,
        session: Any = requests,
        _endpoint: str = DEFAULT_SCHEMA_ENDPOINT,
    ) -> None:
        self.endpoint = _validated_endpoint(_endpoint)
        self.timeout = _validated_timeout(timeout)
        self._session = session

    def fetch(self) -> dict[str, Any]:
        response = self._session.get(
            self.endpoint,
            headers={"Accept": "application/json"},
            timeout=self.timeout,
            stream=True,
            allow_redirects=False,
        )
        try:
            if not 200 <= response.status_code < 300:
                raise ResponseError(f"schema HTTP {response.status_code} error")
            body = _read_limited(response)
            try:
                payload = json.loads(body.decode("utf-8"))
                validate_json_tree(payload)
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                RecursionError,
                TypeError,
                ValueError,
            ) as error:
                raise ResponseError(f"invalid schema JSON response: {error}") from error
            if not isinstance(payload, dict):
                raise ResponseError("schema JSON response must be an object")
            return payload
        finally:
            response.close()


class _BearerAuth(requests.auth.AuthBase):
    """Ensure explicit bearer authentication wins over netrc discovery."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        request.headers["Authorization"] = f"Bearer {self._api_key}"
        return request


class SerpClient:
    """POST bounded, form-encoded SERP requests to Thordata."""

    def __init__(
        self,
        api_key: str,
        timeout: int = 120,
        *,
        session: Any = requests,
        _endpoint: str = DEFAULT_SERP_ENDPOINT,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("API key must be a nonblank string")
        self.api_key = api_key.strip()
        self.endpoint = _validated_endpoint(_endpoint)
        self.timeout = _validated_timeout(timeout)
        self._session = session

    def request(self, form: Mapping[Any, Any]) -> tuple[int, bytes]:
        body = _encode_form(form)
        response = self._session.post(
            self.endpoint,
            auth=_BearerAuth(self.api_key),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "Dify",
                "platform": "dify",
                "api-source": "plugin",
            },
            data=body,
            timeout=self.timeout,
            stream=True,
            allow_redirects=False,
        )
        try:
            return int(response.status_code), _read_limited(response)
        finally:
            response.close()
