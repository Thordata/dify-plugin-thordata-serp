import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest

import thordata_serp.schema as schema_module
from thordata_serp.schema import (
    RESERVED_QUERY_FIELDS,
    SUPPORTED_CONDITION_OPERATORS,
    SUPPORTED_CONTROLS,
    SUPPORTED_VALIDATION_RULES,
    SchemaError,
    SchemaRepository,
    find_engine,
    iter_engines,
    load_snapshot,
    validate_schema_payload,
)


class StubSchemaClient:
    def __init__(self, payload, error=None):
        self.payload = payload
        self.error = error
        self.calls = 0
        self._lock = threading.Lock()

    def fetch(self):
        with self._lock:
            self.calls += 1
            error = self.error
            payload = deepcopy(self.payload)
        if error is not None:
            raise error
        return payload


class SlowSchemaClient(StubSchemaClient):
    def __init__(self, payload, error=None):
        super().__init__(payload, error)
        self.started = threading.Event()
        self.release = threading.Event()

    def fetch(self):
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test did not release schema fetch")
        return super().fetch()


class RecoverySchemaClient(StubSchemaClient):
    def __init__(self, payload):
        super().__init__(payload)
        self.first_started = threading.Event()
        self.first_release = threading.Event()

    def fetch(self):
        with self._lock:
            self.calls += 1
            call = self.calls
            payload = deepcopy(self.payload)
        if call == 1:
            self.first_started.set()
            if not self.first_release.wait(timeout=5):
                raise TimeoutError("test did not release first schema fetch")
            raise OSError("generation one failed")
        return payload


class TrackingCondition(threading.Condition):
    def __init__(self, expected_waiters, pause_after_wake=False):
        super().__init__()
        self.expected_waiters = expected_waiters
        self.pause_after_wake = pause_after_wake
        self.wait_calls = 0
        self.paused_calls = 0
        self.all_waiting = threading.Event()
        self.all_paused = threading.Event()
        self.resume = threading.Event()
        self._wait_calls_lock = threading.Lock()

    def wait(self, timeout=None):
        with self._wait_calls_lock:
            self.wait_calls += 1
            if self.wait_calls == self.expected_waiters:
                self.all_waiting.set()
        result = super().wait(timeout)
        if self.pause_after_wake:
            self.release()
            try:
                with self._wait_calls_lock:
                    self.paused_calls += 1
                    if self.paused_calls == self.expected_waiters:
                        self.all_paused.set()
                if not self.resume.wait(timeout=5):
                    raise TimeoutError("test did not resume schema waiters")
            finally:
                self.acquire()
        return result


def write_snapshot(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def tracked_repository(client, snapshot_path, pause_after_wake=False, **kwargs):
    repository = SchemaRepository(client, snapshot_path, **kwargs)
    condition = TrackingCondition(expected_waiters=7, pause_after_wake=pause_after_wake)
    repository._condition = condition
    return repository, condition


def set_unknown_show_when_operator(payload):
    fields = payload["data"]["categories"][0]["engines"][0]["groups"][0]["fields"]
    fields[1]["show_when"] = {
        "field": "q",
        "operator": "unsupported",
        "values": ["coffee"],
    }


def set_reserved_query_field(payload):
    engine = payload["data"]["categories"][0]["engines"][0]
    engine["groups"][0]["fields"][0]["key"] = "engine"
    engine["query_field"] = "engine"


def set_hidden_query_field(payload):
    payload["data"]["categories"][0]["engines"][0]["groups"][0]["fields"][0]["visible"] = False


def set_conditional_query_field(payload):
    payload["data"]["categories"][0]["engines"][0]["groups"][0]["fields"][0]["show_when"] = {
        "field": "num",
        "values": [10],
    }


INCOMPATIBLE_SCHEMA_MUTATIONS = [
    pytest.param(set_unknown_show_when_operator, id="unknown-operator"),
    pytest.param(set_reserved_query_field, id="reserved-query"),
    pytest.param(set_hidden_query_field, id="hidden-query"),
    pytest.param(set_conditional_query_field, id="conditional-query"),
]


def test_repository_caches_valid_remote_schema_within_ttl(tmp_path, schema_payload):
    clock = [100.0]
    client = StubSchemaClient(schema_payload)
    repository = SchemaRepository(client, tmp_path / "schema.json", ttl=60, clock=lambda: clock[0])

    remote_schema, remote_source = repository.current()
    clock[0] += 10
    cached_schema, cached_source = repository.current()

    assert remote_source == "remote"
    assert cached_source == "cache"
    assert cached_schema == remote_schema
    assert client.calls == 1


def test_repository_returns_stale_cache_when_expired_remote_refresh_fails(tmp_path, schema_payload):
    clock = [100.0]
    client = StubSchemaClient(schema_payload)
    repository = SchemaRepository(client, tmp_path / "schema.json", ttl=60, clock=lambda: clock[0])

    fresh_schema, _ = repository.current()
    clock[0] += 60
    client.error = OSError("upstream unavailable")
    stale_schema, source = repository.current()

    assert source == "cache"
    assert stale_schema == fresh_schema
    assert client.calls == 2


def test_repository_uses_stale_cache_for_invalid_remote_field_type(tmp_path, schema_payload):
    clock = [100.0]
    valid_payload = deepcopy(schema_payload)
    client = StubSchemaClient(schema_payload)
    repository = SchemaRepository(client, tmp_path / "schema.json", ttl=60, clock=lambda: clock[0])
    fresh_schema, _ = repository.current()
    schema_payload["data"]["categories"][0]["engines"][0]["groups"][0]["fields"][0]["type"] = (
        "unsupported"
    )
    clock[0] += 60

    stale_schema, stale_source = repository.current()
    client.payload = valid_payload
    recovered_schema, recovered_source = repository.current()

    assert stale_source == "cache"
    assert stale_schema == fresh_schema
    assert recovered_source == "remote"
    assert recovered_schema == fresh_schema
    assert client.calls == 3


@pytest.mark.parametrize("mutate", INCOMPATIBLE_SCHEMA_MUTATIONS)
def test_repository_uses_stale_cache_for_serializer_incompatible_remote_schema(
    tmp_path, schema_payload, mutate
):
    clock = [100.0]
    valid_payload = deepcopy(schema_payload)
    client = StubSchemaClient(schema_payload)
    repository = SchemaRepository(client, tmp_path / "schema.json", ttl=60, clock=lambda: clock[0])
    fresh_schema, _ = repository.current()
    mutate(schema_payload)
    clock[0] += 60

    stale_schema, stale_source = repository.current()
    client.payload = valid_payload
    recovered_schema, recovered_source = repository.current()

    assert stale_source == "cache"
    assert stale_schema == fresh_schema
    assert recovered_source == "remote"
    assert recovered_schema == fresh_schema
    assert client.calls == 3


def test_repository_uses_snapshot_when_cold_remote_fetch_fails(tmp_path, schema_payload):
    snapshot_path = tmp_path / "schema.json"
    write_snapshot(snapshot_path, schema_payload)
    repository = SchemaRepository(
        StubSchemaClient(schema_payload, error=OSError("upstream unavailable")),
        snapshot_path,
    )

    schema, source = repository.current()

    assert source == "snapshot"
    assert schema["default_engine"] == "google"


def test_repository_uses_snapshot_for_invalid_remote_field_type(tmp_path, schema_payload):
    snapshot_path = tmp_path / "schema.json"
    write_snapshot(snapshot_path, schema_payload)
    remote_payload = deepcopy(schema_payload)
    remote_payload["data"]["categories"][0]["engines"][0]["groups"][0]["fields"][0]["type"] = (
        "unsupported"
    )
    client = StubSchemaClient(remote_payload)
    repository = SchemaRepository(client, snapshot_path)

    schema, source = repository.current()
    cached_schema, cached_source = repository.current()

    assert source == "snapshot"
    assert cached_source == "cache"
    assert cached_schema == schema
    assert client.calls == 1


@pytest.mark.parametrize("mutate", INCOMPATIBLE_SCHEMA_MUTATIONS)
def test_repository_uses_snapshot_for_serializer_incompatible_remote_schema(
    tmp_path, schema_payload, mutate
):
    snapshot_path = tmp_path / "schema.json"
    write_snapshot(snapshot_path, schema_payload)
    remote_payload = deepcopy(schema_payload)
    mutate(remote_payload)
    client = StubSchemaClient(remote_payload)
    repository = SchemaRepository(client, snapshot_path)

    schema, source = repository.current()
    cached_schema, cached_source = repository.current()

    assert source == "snapshot"
    assert cached_source == "cache"
    assert cached_schema == schema
    assert client.calls == 1


def test_repository_shares_one_cold_refresh_across_overlapping_callers(tmp_path, schema_payload):
    client = SlowSchemaClient(schema_payload)
    repository, condition = tracked_repository(client, tmp_path / "schema.json")

    with ThreadPoolExecutor(max_workers=8) as executor:
        leader = executor.submit(repository.current)
        assert client.started.wait(timeout=5)
        followers = [executor.submit(repository.current) for _ in range(7)]
        assert condition.all_waiting.wait(timeout=5)
        client.release.set()
        results = [leader.result(timeout=5), *(future.result(timeout=5) for future in followers)]

    assert client.calls == 1
    assert all(source == "remote" for _, source in results)
    assert all(schema["default_engine"] == "google" for schema, _ in results)
    _, source = repository.current()
    assert source == "cache"


def test_repository_shares_cold_failure_fallback_across_overlapping_callers(
    tmp_path, schema_payload
):
    snapshot_path = tmp_path / "schema.json"
    write_snapshot(snapshot_path, schema_payload)
    client = SlowSchemaClient(schema_payload, error=OSError("upstream unavailable"))
    repository, condition = tracked_repository(client, snapshot_path)

    with ThreadPoolExecutor(max_workers=8) as executor:
        leader = executor.submit(repository.current)
        assert client.started.wait(timeout=5)
        followers = [executor.submit(repository.current) for _ in range(7)]
        assert condition.all_waiting.wait(timeout=5)
        client.release.set()
        results = [leader.result(timeout=5), *(future.result(timeout=5) for future in followers)]

    assert client.calls == 1
    assert all(source == "snapshot" for _, source in results)
    assert all(schema["default_engine"] == "google" for schema, _ in results)
    _, source = repository.current()
    assert source == "cache"


def test_repository_shares_cold_remote_and_snapshot_failure_across_overlapping_callers(
    tmp_path, schema_payload
):
    snapshot_path = tmp_path / "schema.json"
    snapshot_path.write_text("{", encoding="utf-8")
    client = SlowSchemaClient(schema_payload, error=OSError("upstream unavailable"))
    repository, condition = tracked_repository(client, snapshot_path)

    def current_error():
        with pytest.raises(SchemaError) as caught:
            repository.current()
        return caught.value

    with ThreadPoolExecutor(max_workers=8) as executor:
        leader = executor.submit(current_error)
        assert client.started.wait(timeout=5)
        followers = [executor.submit(current_error) for _ in range(7)]
        assert condition.all_waiting.wait(timeout=5)
        client.release.set()
        errors = [leader.result(timeout=5), *(future.result(timeout=5) for future in followers)]

    assert client.calls == 1
    assert len({str(error) for error in errors}) == 1
    assert len({id(error) for error in errors}) == len(errors)
    assert all(error.__cause__ is None for error in errors)
    assert all(error.__context__ is None for error in errors)
    assert "upstream unavailable" in str(errors[0])
    assert "cannot load schema snapshot" in str(errors[0])


def test_repository_retries_after_cold_remote_and_snapshot_failure(tmp_path, schema_payload):
    snapshot_path = tmp_path / "schema.json"
    snapshot_path.write_text("{", encoding="utf-8")
    client = StubSchemaClient(schema_payload, error=OSError("upstream unavailable"))
    repository = SchemaRepository(client, snapshot_path)

    with pytest.raises(SchemaError, match="upstream unavailable.*cannot load schema snapshot"):
        repository.current()

    client.error = None
    schema, source = repository.current()

    assert client.calls == 2
    assert source == "remote"
    assert schema["default_engine"] == "google"


def test_repository_keeps_waiters_bound_to_their_refresh_generation(tmp_path, schema_payload):
    snapshot_path = tmp_path / "schema.json"
    snapshot_path.write_text("{", encoding="utf-8")
    client = RecoverySchemaClient(schema_payload)
    repository, condition = tracked_repository(
        client,
        snapshot_path,
        pause_after_wake=True,
    )

    def current_error():
        with pytest.raises(SchemaError) as caught:
            repository.current()
        return str(caught.value)

    with ThreadPoolExecutor(max_workers=9) as executor:
        leader = executor.submit(current_error)
        assert client.first_started.wait(timeout=5)
        followers = [executor.submit(current_error) for _ in range(7)]
        assert condition.all_waiting.wait(timeout=5)
        client.first_release.set()
        assert condition.all_paused.wait(timeout=5)

        recovery_schema, recovery_source = executor.submit(repository.current).result(timeout=5)
        condition.resume.set()
        leader_error = leader.result(timeout=5)
        follower_errors = [future.result(timeout=5) for future in followers]

    assert client.calls == 2
    assert recovery_source == "remote"
    assert recovery_schema["default_engine"] == "google"
    assert "generation one failed" in leader_error
    assert len(set(follower_errors)) == 1
    assert "generation one failed" in follower_errors[0]
    assert "cannot load schema snapshot" in follower_errors[0]
    _, source = repository.current()
    assert source == "cache"


def test_repository_recovers_from_warm_cache_clock_failure(tmp_path, schema_payload):
    clock = [100.0]
    client = StubSchemaClient(schema_payload)

    def read_clock():
        if isinstance(clock[0], Exception):
            raise clock[0]
        return clock[0]

    repository = SchemaRepository(client, tmp_path / "schema.json", clock=read_clock)

    repository.current()
    clock[0] = RuntimeError("cache clock unavailable")
    with pytest.raises(SchemaError, match="cache clock.*unavailable"):
        repository.current()

    clock[0] = 100.0
    schema, source = repository.current()

    assert source == "cache"
    assert schema["default_engine"] == "google"
    assert client.calls == 1


def test_repository_clears_refresh_when_cache_clock_raises(tmp_path, schema_payload):
    def broken_clock():
        raise RuntimeError("clock unavailable")

    client = SlowSchemaClient(schema_payload)
    repository, condition = tracked_repository(
        client,
        tmp_path / "schema.json",
        clock=broken_clock,
    )

    def current_error():
        with pytest.raises(SchemaError) as caught:
            repository.current()
        return str(caught.value)

    with ThreadPoolExecutor(max_workers=8) as executor:
        leader = executor.submit(current_error)
        assert client.started.wait(timeout=5)
        followers = [executor.submit(current_error) for _ in range(7)]
        assert condition.all_waiting.wait(timeout=5)
        client.release.set()
        errors = [leader.result(timeout=5), *(future.result(timeout=5) for future in followers)]

    assert client.calls == 1
    assert all("clock unavailable" in error for error in errors)
    with ThreadPoolExecutor(max_workers=1) as executor:
        later = executor.submit(current_error)
        assert "clock unavailable" in later.result(timeout=5)


def test_repository_returns_deep_copy_isolated_schemas(tmp_path, schema_payload):
    repository = SchemaRepository(StubSchemaClient(schema_payload), tmp_path / "schema.json")

    first_schema, _ = repository.current()
    first_schema["categories"][0]["engines"][0]["groups"][0]["fields"][0]["key"] = "changed"
    second_schema, _ = repository.current()

    assert second_schema["categories"][0]["engines"][0]["groups"][0]["fields"][0]["key"] == "q"


def test_cached_result_copy_does_not_hold_repository_condition(
    tmp_path, schema_payload, monkeypatch
):
    repository = SchemaRepository(StubSchemaClient(schema_payload), tmp_path / "schema.json")
    repository.current()
    first_copy_started = threading.Event()
    second_copy_started = threading.Event()
    release_first_copy = threading.Event()
    copy_calls = 0
    copy_calls_lock = threading.Lock()

    def blocking_deepcopy(value):
        nonlocal copy_calls
        with copy_calls_lock:
            copy_calls += 1
            call = copy_calls
        if call == 1:
            first_copy_started.set()
            if not release_first_copy.wait(timeout=5):
                raise TimeoutError("test did not release first schema copy")
        else:
            second_copy_started.set()
        return deepcopy(value)

    monkeypatch.setattr(schema_module, "deepcopy", blocking_deepcopy)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(repository.current)
        assert first_copy_started.wait(timeout=5)
        second = executor.submit(repository.current)
        try:
            assert second_copy_started.wait(timeout=1)
        finally:
            release_first_copy.set()
        first_result = first.result(timeout=5)
        second_result = second.result(timeout=5)

    assert first_result == second_result
    assert first_result[1] == "cache"


@pytest.mark.parametrize("ttl", [0, -1, False, True, float("inf"), float("nan")])
def test_repository_rejects_invalid_ttl(tmp_path, schema_payload, ttl):
    with pytest.raises(ValueError, match="ttl"):
        SchemaRepository(StubSchemaClient(schema_payload), tmp_path / "schema.json", ttl=ttl)


def test_valid_envelope_unwraps_and_finds_default_google_engine(schema_payload):
    schema = validate_schema_payload(schema_payload)

    assert schema["default_engine"] == "google"
    assert find_engine(schema, "google")["query_field"] == "q"


def test_direct_engines_work_without_categories(schema_payload):
    schema_payload["data"]["engines"] = schema_payload["data"].pop("categories")[0]["engines"]

    schema = validate_schema_payload(schema_payload)

    assert find_engine(schema, "google")["query_field"] == "q"


def test_iter_engines_yields_category_engines_before_direct_engines(schema_payload):
    direct_engine = deepcopy(schema_payload["data"]["categories"][0]["engines"][0])
    direct_engine["key"] = "bing"
    direct_engine["name"] = "Bing"
    schema_payload["data"]["engines"] = [direct_engine]
    schema = validate_schema_payload(schema_payload)

    engines = list(iter_engines(schema))

    assert [engine["key"] for engine in engines] == ["google", "bing"]


def test_iter_engines_returns_independent_copies(schema_payload):
    schema = validate_schema_payload(schema_payload)

    engine = next(iter_engines(schema))
    engine["groups"][0]["fields"][0]["key"] = "changed"

    assert find_engine(schema, "google")["groups"][0]["fields"][0]["key"] == "q"


def test_query_field_must_reference_a_declared_field(schema_payload):
    schema_payload["data"]["categories"][0]["engines"][0]["query_field"] = "missing"

    with pytest.raises(SchemaError, match="query_field.*declared field"):
        validate_schema_payload(schema_payload)


def test_query_field_must_have_a_query_compatible_type(schema_payload):
    schema_payload["data"]["categories"][0]["engines"][0]["groups"][0]["fields"][0]["type"] = (
        "array"
    )

    with pytest.raises(SchemaError, match="query_field.*compatible type"):
        validate_schema_payload(schema_payload)


@pytest.mark.parametrize("reserved_key", ["engine", "json"])
def test_query_field_must_not_use_reserved_serializer_key(schema_payload, reserved_key):
    engine = schema_payload["data"]["categories"][0]["engines"][0]
    engine["groups"][0]["fields"][0]["key"] = reserved_key
    engine["query_field"] = reserved_key

    with pytest.raises(SchemaError, match="query_field.*reserved"):
        validate_schema_payload(schema_payload)


def test_query_field_must_not_be_explicitly_hidden(schema_payload):
    set_hidden_query_field(schema_payload)

    with pytest.raises(SchemaError, match="query_field.*visible"):
        validate_schema_payload(schema_payload)


def test_query_field_must_not_have_conditional_visibility(schema_payload):
    set_conditional_query_field(schema_payload)

    with pytest.raises(SchemaError, match="query_field.*show_when"):
        validate_schema_payload(schema_payload)


def test_query_field_does_not_need_to_be_required(schema_payload):
    schema_payload["data"]["categories"][0]["engines"][0]["groups"][0]["fields"][0]["required"] = (
        False
    )

    assert validate_schema_payload(schema_payload)["default_engine"] == "google"


def test_query_field_must_declare_visible(schema_payload):
    del schema_payload["data"]["categories"][0]["engines"][0]["groups"][0]["fields"][0]["visible"]

    with pytest.raises(SchemaError, match="visible"):
        validate_schema_payload(schema_payload)


def test_unknown_field_type_is_rejected(schema_payload):
    schema_payload["data"]["categories"][0]["engines"][0]["groups"][0]["fields"][0]["type"] = (
        "unsupported"
    )

    with pytest.raises(SchemaError, match="unsupported field type"):
        validate_schema_payload(schema_payload)


def test_direct_schema_document_is_accepted(schema_payload):
    schema = validate_schema_payload(deepcopy(schema_payload["data"]))

    assert schema["default_engine"] == "google"


def test_validate_schema_payload_returns_an_independent_nested_copy(schema_payload):
    schema = validate_schema_payload(schema_payload)
    schema["categories"][0]["engines"][0]["groups"][0]["fields"][0]["key"] = "changed"

    assert (
        schema_payload["data"]["categories"][0]["engines"][0]["groups"][0]["fields"][0]["key"]
        == "q"
    )


def test_find_engine_returns_an_independent_nested_copy(schema_payload):
    schema = validate_schema_payload(schema_payload)
    engine = find_engine(schema, "google")
    engine["groups"][0]["fields"][0]["key"] = "changed"

    assert schema["categories"][0]["engines"][0]["groups"][0]["fields"][0]["key"] == "q"


def test_business_failure_includes_upstream_code_and_message():
    with pytest.raises(SchemaError, match="401.*denied"):
        validate_schema_payload({"code": 401, "msg": "denied"})


@pytest.mark.parametrize("code", [None, 0, "0", 200, "200"])
def test_successful_business_codes_are_accepted(schema_payload, code):
    schema_payload["code"] = code

    assert validate_schema_payload(schema_payload)["default_engine"] == "google"


def test_envelope_without_business_code_is_accepted(schema_payload):
    schema_payload.pop("code")

    assert validate_schema_payload(schema_payload)["default_engine"] == "google"


@pytest.mark.parametrize(
    "payload",
    [
        {"code": 0},
        {"code": 0, "data": None},
        {"code": 0, "data": []},
        {"data": None},
        {"data": []},
    ],
)
def test_malformed_envelopes_are_rejected(payload):
    with pytest.raises(SchemaError):
        validate_schema_payload(payload)


@pytest.mark.parametrize("code", [False, True, 1, "1", 201, "ok", [], {}])
def test_unsuccessful_business_codes_are_rejected(schema_payload, code):
    schema_payload["code"] = code

    with pytest.raises(SchemaError, match="business code"):
        validate_schema_payload(schema_payload)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda payload: payload["data"].__setitem__("audience", "is_serp_old=1"),
            "unsupported audience",
        ),
        (
            lambda payload: payload["data"].__setitem__("default_engine", "missing"),
            "default engine",
        ),
        (
            lambda payload: payload["data"].__setitem__("schema_version", ""),
            "schema_version",
        ),
    ],
)
def test_invalid_schema_payload_is_rejected(schema_payload, mutate, message):
    mutate(schema_payload)

    with pytest.raises(SchemaError, match=message):
        validate_schema_payload(schema_payload)


@pytest.mark.parametrize(
    ("path", "duplicate"),
    [
        (("categories",), {"key": "google", "name": "Duplicate", "engines": []}),
        (
            ("categories", 0, "engines"),
            {
                "key": "google",
                "name": "Duplicate",
                "query_field": "q",
                "groups": [{"key": "parameters", "fields": [{"key": "q", "type": "string"}]}],
            },
        ),
        (("categories", 0, "engines", 0, "groups"), {"key": "parameters", "fields": []}),
        (
            ("categories", 0, "engines", 0, "groups", 0, "fields"),
            {"key": "q", "type": "string"},
        ),
    ],
)
def test_duplicate_keys_are_rejected_at_each_scope(schema_payload, path, duplicate):
    target = schema_payload["data"]
    for component in path:
        target = target[component]
    target.append(duplicate)

    with pytest.raises(SchemaError, match="duplicate"):
        validate_schema_payload(schema_payload)


def test_show_when_references_field_in_same_engine(schema_payload):
    field = schema_payload["data"]["categories"][0]["engines"][0]["groups"][0]["fields"][1]
    field["show_when"] = {"field": "missing", "operator": "equals", "values": [1, "1", True]}

    with pytest.raises(SchemaError, match="show_when.field.*same engine"):
        validate_schema_payload(schema_payload)


@pytest.mark.parametrize("operator", ["equals", "not_equals", "in", "not_in"])
def test_show_when_accepts_serializer_supported_operators(schema_payload, operator):
    condition = {"field": "q", "operator": operator, "values": ["coffee"]}
    schema_payload["data"]["categories"][0]["engines"][0]["groups"][0]["fields"][1]["show_when"] = (
        condition
    )

    assert validate_schema_payload(schema_payload)["default_engine"] == "google"


def test_show_when_rejects_unknown_operator(schema_payload):
    set_unknown_show_when_operator(schema_payload)

    with pytest.raises(SchemaError, match="show_when.operator.*unsupported"):
        validate_schema_payload(schema_payload)


@pytest.mark.parametrize("attribute", ["default", "default_value"])
def test_string_field_defaults_must_be_strings(schema_payload, attribute):
    field = schema_payload["data"]["categories"][0]["engines"][0]["groups"][0]["fields"][0]
    field[attribute] = 1

    with pytest.raises(SchemaError, match=f"field {attribute} has invalid shape"):
        validate_schema_payload(schema_payload)


@pytest.mark.parametrize("unsafe", [float("nan"), float("inf"), -float("inf")])
def test_schema_rejects_nonfinite_json_numbers(schema_payload, unsafe):
    schema_payload["data"]["categories"][0]["engines"][0]["groups"][0]["fields"][1]["default"] = (
        unsafe
    )

    with pytest.raises(SchemaError, match="invalid schema JSON"):
        validate_schema_payload(schema_payload)


def test_schema_rejects_non_string_json_keys(schema_payload):
    schema_payload["data"][1] = "invalid"

    with pytest.raises(SchemaError, match="invalid schema JSON"):
        validate_schema_payload(schema_payload)


def test_schema_rejects_cycles(schema_payload):
    schema_payload["cycle"] = schema_payload

    with pytest.raises(SchemaError, match="invalid schema JSON"):
        validate_schema_payload(schema_payload)


def test_schema_rejects_excessive_nesting(schema_payload):
    nested = []
    cursor = nested
    for _ in range(257):
        child = []
        cursor.append(child)
        cursor = child
    schema_payload["nested"] = nested

    with pytest.raises(SchemaError, match="invalid schema JSON"):
        validate_schema_payload(schema_payload)


def test_package_snapshot_loads_and_declares_its_default_engine():
    snapshot_path = Path(__file__).parents[1] / "thordata_serp/serp-schema.snapshot.json"

    schema = load_snapshot(snapshot_path)

    assert schema["audience"] == "is_serp_old=0"
    assert schema["default_engine"] == "google"
    assert list(iter_engines(schema))
    assert find_engine(schema, schema["default_engine"])["key"] == "google"


def test_load_snapshot_rejects_invalid_utf8(tmp_path):
    snapshot_path = tmp_path / "invalid.json"
    snapshot_path.write_bytes(b"\xff")

    with pytest.raises(SchemaError, match="cannot load schema snapshot"):
        load_snapshot(snapshot_path)


@pytest.mark.parametrize(
    "attribute,value",
    [
        ("type", []),
        ("type", None),
        ("control", {}),
        ("visible", "true"),
        ("required", 1),
        ("options", {}),
        ("options", ["bad"]),
        ("options", [{"value": "x"}]),
        ("options", [{"label": "x"}]),
        ("options", [{"label": [], "value": "x"}]),
        ("options", [{"label": "x", "value": {}}]),
        ("default", ["bad"]),
        ("default_value", {"bad": 1}),
        ("name", []),
        ("group", {}),
        ("show_when", {"field": "num", "operator": []}),
        ("show_when", {"field": "num", "values": {}}),
    ],
)
def test_rejects_malformed_field_structure(schema_payload, attribute, value):
    field = schema_payload["data"]["categories"][0]["engines"][0]["groups"][0]["fields"][0]
    field[attribute] = value
    with pytest.raises(SchemaError):
        validate_schema_payload(schema_payload)


def test_n8n_field_metadata_and_validation_rules_are_preserved(schema_payload):
    engine = schema_payload["data"]["categories"][0]["engines"][0]
    fields = engine["groups"][0]["fields"]
    fields[1].update(
        {
            "control": "number",
            "default_value": 10,
            "placeholder": "count",
            "description": "Result count.",
            "show_when": {
                "field": "q",
                "operator": "equals",
                "values": ["coffee"],
            },
        }
    )
    engine["validation_rules"] = [{"type": "mutually_exclusive", "fields": ["q", "num"]}]

    validated = validate_schema_payload(schema_payload)

    assert (
        validated["categories"][0]["engines"][0]["validation_rules"] == engine["validation_rules"]
    )
    assert validated["categories"][0]["engines"][0]["groups"][0]["fields"][1] == fields[1]


def test_schema_constants_describe_n8n_contract():
    assert SUPPORTED_CONTROLS == {
        "input": "string",
        "number": "number",
        "select": "options",
        "multi_select": "array",
        "switch": "boolean",
        "json": "object",
    }
    assert {"equals", "not_equals", "in", "not_in"} == SUPPORTED_CONDITION_OPERATORS
    assert {"required_any_of", "mutually_exclusive"} == SUPPORTED_VALIDATION_RULES
    assert {"engine", "json", "isjson"} == RESERVED_QUERY_FIELDS


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("control", "country"),
        ("control", None),
        ("type", "cascader"),
        ("type", "switch"),
    ],
)
def test_n8n_rejects_unsupported_or_missing_control(schema_payload, attribute, value):
    field = schema_payload["data"]["categories"][0]["engines"][0]["groups"][0]["fields"][0]
    if attribute == "control" and value is None:
        field.pop("control")
    else:
        field[attribute] = value

    with pytest.raises(SchemaError):
        validate_schema_payload(schema_payload)


def test_non_query_control_type_mismatch_reports_field_error(schema_payload):
    field = schema_payload["data"]["categories"][0]["engines"][0]["groups"][0]["fields"][1]
    field["control"] = "input"

    with pytest.raises(SchemaError, match="field control does not match field type"):
        validate_schema_payload(schema_payload)


@pytest.mark.parametrize("metadata_key", ["label", "visible", "required"])
def test_n8n_field_metadata_is_required(schema_payload, metadata_key):
    field = schema_payload["data"]["categories"][0]["engines"][0]["groups"][0]["fields"][1]
    field.pop(metadata_key, None)

    with pytest.raises(SchemaError, match=metadata_key):
        validate_schema_payload(schema_payload)


@pytest.mark.parametrize("location", ["category", "group"])
def test_n8n_category_and_group_names_are_required(schema_payload, location):
    category = schema_payload["data"]["categories"][0]
    if location == "category":
        category.pop("name")
    else:
        category["engines"][0]["groups"][0].pop("name")

    with pytest.raises(SchemaError, match="name"):
        validate_schema_payload(schema_payload)


@pytest.mark.parametrize(
    "value",
    [None, "", [], [{"label": "x", "value": "a"}, {"label": "y", "value": "a"}]],
)
def test_n8n_rejects_empty_or_duplicate_options(schema_payload, value):
    field = schema_payload["data"]["categories"][0]["engines"][0]["groups"][0]["fields"][1]
    field["type"] = "options"
    field["control"] = "select"
    field["options"] = value

    with pytest.raises(SchemaError):
        validate_schema_payload(schema_payload)


@pytest.mark.parametrize(
    ("attribute", "value"),
    [("default_value", "ten"), ("default", float("inf"))],
)
def test_n8n_rejects_incompatible_defaults(schema_payload, attribute, value):
    field = schema_payload["data"]["categories"][0]["engines"][0]["groups"][0]["fields"][1]
    field[attribute] = value

    with pytest.raises(SchemaError):
        validate_schema_payload(schema_payload)


def test_n8n_multi_select_defaults_must_match_typed_options(schema_payload):
    field = schema_payload["data"]["categories"][0]["engines"][0]["groups"][0]["fields"][1]
    field.update(
        {
            "type": "array",
            "control": "multi_select",
            "options": [{"label": "One", "value": 1}],
            "default_value": ["1"],
        }
    )

    with pytest.raises(SchemaError):
        validate_schema_payload(schema_payload)


def test_legacy_field_does_not_relax_modern_field_validation(schema_payload):
    fields = schema_payload["data"]["categories"][0]["engines"][0]["groups"][0]["fields"]
    fields.extend(
        [
            {"key": "modern_missing_control", "type": "string"},
            {"key": "legacy_place", "type": "cascader"},
        ]
    )

    with pytest.raises(SchemaError, match="control"):
        validate_schema_payload(schema_payload)


@pytest.mark.parametrize(
    "option",
    [
        {"label": "", "value": "x"},
        {"label": "x", "value": "x", "extra": True},
        {"label": "x", "value": "x", "children": []},
    ],
)
def test_n8n_options_use_strict_option_objects(schema_payload, option):
    field = schema_payload["data"]["categories"][0]["engines"][0]["groups"][0]["fields"][1]
    field.update({"type": "options", "control": "select", "options": [option]})
    field.pop("default", None)

    with pytest.raises(SchemaError):
        validate_schema_payload(schema_payload)


def test_n8n_show_when_rejects_unknown_keys_but_accepts_null_values(schema_payload):
    field = schema_payload["data"]["categories"][0]["engines"][0]["groups"][0]["fields"][1]
    field["show_when"] = {
        "field": "q",
        "operator": "equals",
        "values": [None],
        "extra": True,
    }

    with pytest.raises(SchemaError):
        validate_schema_payload(schema_payload)

    field["show_when"].pop("extra")
    assert validate_schema_payload(schema_payload)["default_engine"] == "google"


@pytest.mark.parametrize("dangerous_key", ["__proto__", "prototype", "constructor"])
@pytest.mark.parametrize(
    "location",
    [
        "category",
        "engine",
        "group",
        "field",
        "option",
        "show_when",
        "validation_rule",
    ],
)
def test_schema_rejects_dangerous_keys_in_all_schema_graph_locations(
    schema_payload, dangerous_key, location
):
    category = schema_payload["data"]["categories"][0]
    engine = category["engines"][0]
    group = engine["groups"][0]
    field = group["fields"][1]
    if location == "category":
        category["key"] = dangerous_key
    elif location == "engine":
        engine["key"] = dangerous_key
    elif location == "group":
        group["key"] = dangerous_key
    elif location == "field":
        field["key"] = dangerous_key
    elif location == "option":
        field.update(
            {
                "type": "options",
                "control": "select",
                "options": [{"label": "safe", "value": "safe", dangerous_key: True}],
            }
        )
        field.pop("default", None)
    elif location == "show_when":
        field["show_when"] = {
            "field": dangerous_key,
            "operator": "equals",
            "values": ["safe"],
        }
    else:
        engine["validation_rules"] = [{"type": "required_any_of", "fields": ["q", dangerous_key]}]

    with pytest.raises(SchemaError):
        validate_schema_payload(schema_payload)


@pytest.mark.parametrize("dangerous_key", ["__proto__", "prototype", "constructor"])
def test_schema_rejects_dangerous_keys_in_unknown_raw_mappings(schema_payload, dangerous_key):
    schema_payload[dangerous_key] = {"safe": True}
    schema_payload["data"]["unknown"] = {dangerous_key: True}
    schema_payload["data"]["categories"][0]["engines"][0]["metadata"] = {dangerous_key: True}

    with pytest.raises(SchemaError):
        validate_schema_payload(schema_payload)


def test_validation_rules_must_be_an_array(schema_payload):
    schema_payload["data"]["categories"][0]["engines"][0]["validation_rules"] = {
        "type": "mutually_exclusive",
        "fields": ["q", "num"],
    }

    with pytest.raises(SchemaError, match="validation_rules.*array"):
        validate_schema_payload(schema_payload)


@pytest.mark.parametrize(
    "condition",
    [
        {"field": "q", "operator": "equals", "values": []},
        {"field": "q", "operator": "equals", "values": [{}]},
        {"field": "q", "operator": "unknown", "values": ["coffee"]},
        {"field": "q", "values": ["coffee"]},
    ],
)
def test_n8n_rejects_malformed_show_when(schema_payload, condition):
    field = schema_payload["data"]["categories"][0]["engines"][0]["groups"][0]["fields"][1]
    field["show_when"] = condition

    with pytest.raises(SchemaError):
        validate_schema_payload(schema_payload)


@pytest.mark.parametrize(
    "rules",
    [
        {"type": "unknown", "fields": ["q", "num"]},
        {"type": "required_any_of", "fields": ["q", "q"]},
        {"type": "mutually_exclusive", "fields": ["q"]},
        {"type": "mutually_exclusive", "fields": ["q", "missing"]},
    ],
)
def test_n8n_rejects_malformed_validation_rules(schema_payload, rules):
    schema_payload["data"]["categories"][0]["engines"][0]["validation_rules"] = [rules]

    with pytest.raises(SchemaError):
        validate_schema_payload(schema_payload)


@pytest.mark.parametrize("reserved_key", ["engine", "json", "isjson"])
def test_n8n_rejects_all_reserved_query_fields(schema_payload, reserved_key):
    engine = schema_payload["data"]["categories"][0]["engines"][0]
    engine["groups"][0]["fields"][0]["key"] = reserved_key
    engine["query_field"] = reserved_key

    with pytest.raises(SchemaError):
        validate_schema_payload(schema_payload)


def test_snapshot_matches_n8n_and_exposes_key_metadata():
    local_path = Path(__file__).parents[1] / "thordata_serp/serp-schema.snapshot.json"
    n8n_path = (
        Path(__file__).parents[3]
        / "n8n/n8n-nodes-thordata/nodes/Thordata/serp-schema.snapshot.json"
    )
    assert (
        hashlib.sha256(local_path.read_bytes()).digest()
        == hashlib.sha256(n8n_path.read_bytes()).digest()
    )

    schema = load_snapshot(local_path)
    engines = list(iter_engines(schema))
    assert schema["audience"] == "is_serp_old=0"
    assert schema["default_engine"] == "google"
    assert len(engines) == 34
    google_web = find_engine(schema, "google_web")
    fields = {field["key"]: field for group in google_web["groups"] for field in group["fields"]}
    assert fields["cr"]["control"] == "multi_select"
    assert fields["lr"]["control"] == "multi_select"
    assert fields["location"]["visible"] is True
    assert fields["uule"]["visible"] is True
    season = find_engine(schema, "google_play_product")
    season_field = next(
        field
        for group in season["groups"]
        for field in group["fields"]
        if field["key"] == "season_id"
    )
    assert season_field["show_when"]["operator"] == "equals"
