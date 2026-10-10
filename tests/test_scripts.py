import errno
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import smoke_serp, update_schema_snapshot  # noqa: E402
from thordata_serp.response import ResponseError, SerpApiError  # noqa: E402
from thordata_serp.schema import SchemaError  # noqa: E402
from thordata_serp.serializer import ParameterError  # noqa: E402

DEFAULT_QUERY = "Dify Thordata integration smoke test"
EXPECTED_SEARCH = {
    "engine": "google",
    "query": DEFAULT_QUERY,
    "parameters_json": '{"num":1}',
    "response_format": "1",
    "response_mode": "compact",
}


def test_maintenance_docs_keep_smoke_credentials_out_of_process_arguments() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "THORDATA_SERP_API_KEY" in readme
    assert "--api-key" not in readme
    assert "POSIX" in readme
    assert "PowerShell" in readme
    assert "cmd.exe" in readme
    assert "python scripts/smoke_serp.py" in readme


def test_privacy_names_every_value_excluded_from_independent_logging() -> None:
    privacy = " ".join((ROOT / "PRIVACY.md").read_text(encoding="utf-8").lower().split())

    assert (
        "does not independently log the api key, query, selected engine, search parameters, "
        "or results"
    ) in privacy


class StubSchemaClient:
    def __init__(self, payload: object = None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls = 0

    def fetch(self) -> object:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.payload


class RecordingService:
    def __init__(
        self,
        *,
        source: str = "remote",
        data: object = None,
        status: int = 200,
        engine: str = "google",
        fail_at: str | None = None,
        error: Exception | None = None,
    ) -> None:
        self.source = source
        self.data = {} if data is None else data
        self.status = status
        self.engine = engine
        self.fail_at = fail_at
        self.error = error or SchemaError("stage failed")
        self.calls: list[tuple[str, object]] = []

    def _record(self, stage: str, parameters: object = None) -> None:
        self.calls.append((stage, parameters))
        if self.fail_at == stage:
            raise self.error

    def list_engines(self) -> dict[str, object]:
        self._record("list")
        return {
            "ok": True,
            "schema_source": self.source,
            "default_engine": self.engine,
            "engines": [{"key": self.engine}],
        }

    def get_engine_schema(self, engine: str) -> dict[str, object]:
        self._record("schema", engine)
        return {"ok": True, "engine": {"key": engine}}

    def search(self, **parameters: object) -> dict[str, object]:
        self._record("search", parameters)
        return {
            "ok": True,
            "status": self.status,
            "engine": parameters["engine"],
            "data": self.data,
        }


class BrokenStringError(RuntimeError):
    def __str__(self) -> str:
        raise RuntimeError("string conversion failed")


class NoFullScanString(str):
    def replace(self, old: str, new: str, count: int = -1) -> str:
        raise AssertionError("summary rendering must not replace across the full engine")


class BoundedMatchString(NoFullScanString):
    comparison_lengths: list[int]

    def __new__(cls, value: str) -> "BoundedMatchString":
        instance = super().__new__(cls, value)
        instance.comparison_lengths = []
        return instance

    def startswith(
        self,
        prefix: str | tuple[str, ...],
        start: int = 0,
        end: int | None = None,
    ) -> bool:
        prefixes = prefix if isinstance(prefix, tuple) else (prefix,)
        self.comparison_lengths.extend(len(item) for item in prefixes)
        if any(len(item) > 128 for item in prefixes):
            raise AssertionError("sensitive comparison exceeded the summary source budget")
        if end is None:
            return super().startswith(prefix, start)
        return super().startswith(prefix, start, end)


def _engine_count(schema: dict[str, Any]) -> int:
    categories = schema.get("categories", [])
    return sum(len(category.get("engines", [])) for category in categories) + len(
        schema.get("engines", [])
    )


def test_default_snapshot_target_is_the_bundled_dify_snapshot() -> None:
    assert update_schema_snapshot.DEFAULT_TARGET == (
        ROOT / "thordata_serp" / "serp-schema.snapshot.json"
    )


def test_maintenance_scripts_show_help_from_isolated_external_cwd(tmp_path: Path) -> None:
    for script_name in ("update_schema_snapshot.py", "smoke_serp.py"):
        completed = subprocess.run(
            [sys.executable, "-I", str(ROOT / "scripts" / script_name), "--help"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )

        assert completed.returncode == 0, completed.stderr
        assert f"usage: {script_name}" in completed.stdout
        if script_name == "smoke_serp.py":
            assert "process listings" in completed.stdout
            assert "shell history" in completed.stdout
            assert "THORDATA_SERP_API_KEY" in completed.stdout


def test_update_snapshot_validates_writes_unicode_and_reloads(
    tmp_path: Path, schema_payload: dict[str, Any]
) -> None:
    target = tmp_path / "nested" / "snapshot.json"
    schema_payload["msg"] = "caf\u00e9"

    schema = update_schema_snapshot.update_snapshot(StubSchemaClient(schema_payload), target)

    assert schema["default_engine"] == "google"
    assert json.loads(target.read_text(encoding="utf-8")) == schema_payload
    assert b"caf\xc3\xa9" in target.read_bytes()
    assert target.read_bytes().endswith(b"\n")
    assert not list(target.parent.glob(f"{target.name}.*.tmp"))


@pytest.mark.parametrize(
    "client",
    [
        StubSchemaClient({"code": 401, "msg": "denied"}),
        StubSchemaClient(error=OSError("offline")),
    ],
)
def test_update_snapshot_preserves_existing_bytes_on_fetch_or_validation_failure(
    tmp_path: Path, client: StubSchemaClient
) -> None:
    target = tmp_path / "snapshot.json"
    original = b"old snapshot\x00\xff"
    target.write_bytes(original)

    with pytest.raises((OSError, SchemaError)):
        update_schema_snapshot.update_snapshot(client, target)

    assert target.read_bytes() == original
    assert not list(tmp_path.glob(f"{target.name}.*.tmp"))


def test_update_snapshot_cleans_partial_temp_after_write_failure(
    tmp_path: Path,
    schema_payload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "snapshot.json"
    original = b"old snapshot"
    target.write_bytes(original)

    def fail_dump(payload: object, handle: Any, **kwargs: object) -> None:
        handle.write("partial")
        handle.flush()
        raise OSError("write failed")

    monkeypatch.setattr(update_schema_snapshot.json, "dump", fail_dump)

    with pytest.raises(OSError, match="write failed"):
        update_schema_snapshot.update_snapshot(StubSchemaClient(schema_payload), target)

    assert target.read_bytes() == original
    assert not list(tmp_path.glob(f"{target.name}.*.tmp"))


def test_update_snapshot_syncs_file_before_replace_and_directory_after(
    tmp_path: Path,
    schema_payload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "snapshot.json"
    events: list[str] = []
    real_replace = os.replace

    def tracked_replace(source: str | Path, destination: str | Path) -> None:
        events.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr(update_schema_snapshot.os, "fsync", lambda fd: events.append("file_fsync"))
    monkeypatch.setattr(update_schema_snapshot.os, "replace", tracked_replace)
    monkeypatch.setattr(
        update_schema_snapshot,
        "_fsync_directory",
        lambda directory: events.append("directory_fsync"),
    )

    update_schema_snapshot.update_snapshot(StubSchemaClient(schema_payload), target)

    assert events == ["file_fsync", "replace", "directory_fsync"]


def test_update_snapshot_temp_creation_failure_never_deletes_foreign_file(
    tmp_path: Path,
    schema_payload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "snapshot.json"
    foreign = tmp_path / "snapshot.json.foreign.tmp"
    foreign.write_bytes(b"foreign")

    def fail_mkstemp(*, prefix: str, suffix: str, dir: Path) -> tuple[int, str]:
        assert prefix == "snapshot.json."
        assert suffix == ".tmp"
        assert Path(dir) == tmp_path
        raise FileExistsError("simulated collision")

    monkeypatch.setattr(update_schema_snapshot.tempfile, "mkstemp", fail_mkstemp)

    with pytest.raises(FileExistsError, match="simulated collision"):
        update_schema_snapshot.update_snapshot(StubSchemaClient(schema_payload), target)

    assert foreign.read_bytes() == b"foreign"
    assert not target.exists()


def test_update_snapshot_uses_sibling_temp_and_cleans_it_when_replace_fails(
    tmp_path: Path,
    schema_payload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "snapshot.json"
    original = b"old snapshot"
    target.write_bytes(original)
    observed: list[tuple[Path, Path, bool]] = []

    def fail_replace(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        observed.append((source_path, destination_path, source_path.is_file()))
        raise OSError("replace failed")

    monkeypatch.setattr(update_schema_snapshot.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        update_schema_snapshot.update_snapshot(StubSchemaClient(schema_payload), target)

    assert observed and observed[0][0].parent == target.parent
    assert observed[0][1] == target
    assert observed[0][2] is True
    assert target.read_bytes() == original
    assert not list(tmp_path.glob(f"{target.name}.*.tmp"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are not portable on Windows")
def test_update_snapshot_preserves_existing_target_mode(
    tmp_path: Path, schema_payload: dict[str, Any]
) -> None:
    target = tmp_path / "snapshot.json"
    target.write_bytes(b"old snapshot")
    target.chmod(0o640)

    update_schema_snapshot.update_snapshot(StubSchemaClient(schema_payload), target)

    assert stat.S_IMODE(target.stat().st_mode) == 0o640


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are not portable on Windows")
def test_update_snapshot_new_target_has_explicit_public_read_mode(
    tmp_path: Path, schema_payload: dict[str, Any]
) -> None:
    target = tmp_path / "snapshot.json"

    update_schema_snapshot.update_snapshot(StubSchemaClient(schema_payload), target)

    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_directory_fsync_uses_directory_descriptor_on_supported_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, object]] = []

    monkeypatch.setattr(update_schema_snapshot, "_DIRECTORY_FSYNC_SUPPORTED", True)
    monkeypatch.setattr(
        update_schema_snapshot.os,
        "open",
        lambda path, flags: events.append(("open", (Path(path), flags))) or 73,
    )
    monkeypatch.setattr(
        update_schema_snapshot.os,
        "fsync",
        lambda descriptor: events.append(("fsync", descriptor)),
    )
    monkeypatch.setattr(
        update_schema_snapshot.os,
        "close",
        lambda descriptor: events.append(("close", descriptor)),
    )

    update_schema_snapshot._fsync_directory(tmp_path)

    assert [event[0] for event in events] == ["open", "fsync", "close"]
    assert events[0][1][0] == tmp_path
    assert events[1][1] == 73
    assert events[2][1] == 73


@pytest.mark.parametrize("error_number", [errno.EINVAL, errno.ENOTSUP])
def test_directory_fsync_ignores_only_unsupported_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_number: int
) -> None:
    closed: list[int] = []
    monkeypatch.setattr(update_schema_snapshot, "_DIRECTORY_FSYNC_SUPPORTED", True)
    monkeypatch.setattr(update_schema_snapshot.os, "open", lambda path, flags: 79)
    monkeypatch.setattr(
        update_schema_snapshot.os,
        "fsync",
        lambda descriptor: (_ for _ in ()).throw(OSError(error_number, "unsupported")),
    )
    monkeypatch.setattr(update_schema_snapshot.os, "close", closed.append)

    update_schema_snapshot._fsync_directory(tmp_path)

    assert closed == [79]


def test_directory_fsync_propagates_ordinary_posix_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[int] = []
    monkeypatch.setattr(update_schema_snapshot, "_DIRECTORY_FSYNC_SUPPORTED", True)
    monkeypatch.setattr(update_schema_snapshot.os, "open", lambda path, flags: 83)
    monkeypatch.setattr(
        update_schema_snapshot.os,
        "fsync",
        lambda descriptor: (_ for _ in ()).throw(OSError(errno.EIO, "disk failure")),
    )
    monkeypatch.setattr(update_schema_snapshot.os, "close", closed.append)

    with pytest.raises(OSError, match="disk failure"):
        update_schema_snapshot._fsync_directory(tmp_path)

    assert closed == [83]


def test_directory_fsync_failure_after_replace_does_not_roll_back_new_target(
    tmp_path: Path,
    schema_payload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "snapshot.json"
    target.write_bytes(b"old snapshot")
    monkeypatch.setattr(
        update_schema_snapshot,
        "_fsync_directory",
        lambda directory: (_ for _ in ()).throw(OSError(errno.EIO, "directory sync failed")),
    )

    with pytest.raises(OSError, match="directory sync failed"):
        update_schema_snapshot.update_snapshot(StubSchemaClient(schema_payload), target)

    assert json.loads(target.read_text(encoding="utf-8")) == schema_payload
    assert not list(tmp_path.glob(f"{target.name}.*.tmp"))


def test_update_main_constructs_fixed_client_and_prints_only_summary(
    tmp_path: Path,
    schema_payload: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "snapshot.json"
    created: list[int] = []

    def factory(timeout: int) -> StubSchemaClient:
        created.append(timeout)
        return StubSchemaClient(schema_payload)

    result = update_schema_snapshot.main(
        ["--target", str(target), "--timeout", "17"], client_factory=factory
    )

    assert result == 0
    assert created == [17]
    assert capsys.readouterr().out.strip() == (
        f"schema_version=1 engines={_engine_count(schema_payload['data'])}"
    )


def test_update_main_returns_safe_error_without_payload_or_target_change(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "snapshot.json"
    original = b"old snapshot"
    target.write_bytes(original)
    sensitive = "synthetic-secret-payload"

    result = update_schema_snapshot.main(
        ["--target", str(target)],
        client_factory=lambda timeout: StubSchemaClient({"code": 401, "msg": sensitive}),
    )

    output = capsys.readouterr().out.strip()
    assert result == 1
    assert output == "error=SchemaError"
    assert sensitive not in output
    assert target.read_bytes() == original


def test_update_main_bounds_and_escapes_hostile_schema_version(
    tmp_path: Path,
    schema_payload: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    hostile_version = "v1 version\n\x01\x7f" + "\u754c" * 200
    schema_payload["data"]["schema_version"] = hostile_version

    result = update_schema_snapshot.main(
        ["--target", str(tmp_path / "snapshot.json")],
        client_factory=lambda timeout: StubSchemaClient(schema_payload),
    )

    output = capsys.readouterr().out
    assert result == 0
    assert output.count("\n") == 1
    line = output.rstrip("\n")
    fields = line.split()
    assert line.isascii()
    assert len(line) <= 180
    assert len(fields) == 2
    assert not any(character.isspace() for character in fields[0])
    assert all(32 <= ord(character) < 127 for character in line)
    assert "v1\\u0020version\\n\\u0001\\u007f" in fields[0]
    assert len(fields[0].removeprefix("schema_version=")) <= 128
    assert fields[0].endswith("...")
    assert hostile_version not in line


def test_update_summary_encoding_stops_at_bounded_budget_for_large_version(
    tmp_path: Path,
    schema_payload: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema_payload["data"]["schema_version"] = "large version\x7f" + "x" * 2_000_000
    dumps_calls = 0
    original_dumps = update_schema_snapshot.json.dumps

    def counting_dumps(value: object, *args: object, **kwargs: object) -> str:
        nonlocal dumps_calls
        dumps_calls += 1
        if dumps_calls > 256:
            raise AssertionError("summary encoding exceeded its bounded call budget")
        return original_dumps(value, *args, **kwargs)

    monkeypatch.setattr(update_schema_snapshot.json, "dumps", counting_dumps)

    result = update_schema_snapshot.main(
        ["--target", str(tmp_path / "snapshot.json")],
        client_factory=lambda timeout: StubSchemaClient(schema_payload),
    )

    output = capsys.readouterr().out.strip()
    assert result == 0
    assert dumps_calls <= 129
    assert len(output) <= 180
    assert output.split()[-1] == "engines=1"


@pytest.mark.parametrize("timeout", ["0", "-1"])
def test_update_main_rejects_nonpositive_timeout(timeout: str) -> None:
    with pytest.raises(SystemExit) as captured:
        update_schema_snapshot.main(["--timeout", timeout])

    assert captured.value.code == 2


def test_update_main_does_not_offer_an_endpoint_override() -> None:
    with pytest.raises(SystemExit) as captured:
        update_schema_snapshot.main(
            ["--endpoint", "https://example.test/schema"],
            client_factory=lambda timeout: pytest.fail("client must not be constructed"),
        )

    assert captured.value.code == 2


@pytest.mark.parametrize("value", [None, "", " \t\r\n"])
def test_smoke_requires_nonblank_key_without_constructing_service(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str | None,
) -> None:
    if value is None:
        monkeypatch.delenv("THORDATA_SERP_API_KEY", raising=False)
        argv: list[str] = []
    else:
        monkeypatch.setenv("THORDATA_SERP_API_KEY", value)
        argv = []

    result = smoke_serp.main(
        argv, service_factory=lambda key: pytest.fail("service must not be constructed")
    )

    assert result == 2
    assert capsys.readouterr().out.strip() == "THORDATA_SERP_API_KEY is required"


@pytest.mark.parametrize(
    ("source", "data", "expected_size"),
    [
        ("remote", {"organic": [], "news": []}, 2),
        ("cache", [1, 2, 3], 3),
        ("snapshot", "not structural", 0),
        ("remote", None, 0),
    ],
)
def test_smoke_calls_exact_service_surface_and_prints_one_safe_summary(
    capsys: pytest.CaptureFixture[str],
    source: str,
    data: object,
    expected_size: int,
) -> None:
    service = RecordingService(source=source, data=data, status=201)
    built_with: list[str] = []

    def factory(api_key: str) -> RecordingService:
        built_with.append(api_key)
        return service

    result = smoke_serp.main(["--api-key", "  synthetic-key  "], service_factory=factory)

    assert result == 0
    assert built_with == ["synthetic-key"]
    assert service.calls == [
        ("list", None),
        ("schema", "google"),
        ("search", EXPECTED_SEARCH),
    ]
    assert capsys.readouterr().out.strip() == (
        f"ok=true schema_source={source} engine=google status=201 result_size={expected_size}"
    )


def test_smoke_honors_environment_key_and_query_override(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("THORDATA_SERP_API_KEY", "environment-key")
    service = RecordingService(data=[])
    built_with: list[str] = []

    def factory(api_key: str) -> RecordingService:
        built_with.append(api_key)
        return service

    result = smoke_serp.main(["--query", "tea"], service_factory=factory)

    assert result == 0
    assert built_with == ["environment-key"]
    assert service.calls[-1][1] == {**EXPECTED_SEARCH, "query": "tea"}
    assert "environment-key" not in capsys.readouterr().out


def test_smoke_uses_original_hostile_engine_but_prints_bounded_ascii_token(
    capsys: pytest.CaptureFixture[str],
) -> None:
    api_key = "synthetic-key-sentinel"
    query = "query-sentinel"
    result_marker = "result-sentinel"
    engine = " \nengine space\x01\x7f" + "\u754c" * 200 + "\t "
    service = RecordingService(engine=engine, data={result_marker: [1]})

    result = smoke_serp.main(
        ["--api-key", api_key, "--query", query], service_factory=lambda key: service
    )

    output = capsys.readouterr().out
    assert result == 0
    assert service.calls == [
        ("list", None),
        ("schema", engine),
        ("search", {**EXPECTED_SEARCH, "engine": engine, "query": query}),
    ]
    assert output.count("\n") == 1
    line = output.rstrip("\n")
    fields = line.split()
    assert line.isascii()
    assert len(line) <= 220
    assert len(fields) == 5
    assert not any(character.isspace() for character in fields[2])
    assert all(32 <= ord(character) < 127 for character in line)
    assert "engine=\\u0020\\nengine\\u0020space\\u0001\\u007f" in fields[2]
    assert len(fields[2].removeprefix("engine=")) <= 128
    assert fields[2].endswith("...")
    assert api_key not in line
    assert query not in line
    assert result_marker not in line


def test_smoke_summary_encoding_stops_without_scanning_large_engine(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = NoFullScanString("large engine\x7f" + "x" * 2_000_000)
    service = RecordingService(engine=engine, data=[])
    dumps_calls = 0
    original_dumps = smoke_serp.json.dumps

    def counting_dumps(value: object, *args: object, **kwargs: object) -> str:
        nonlocal dumps_calls
        dumps_calls += 1
        if dumps_calls > 256:
            raise AssertionError("summary encoding exceeded its bounded call budget")
        return original_dumps(value, *args, **kwargs)

    monkeypatch.setattr(smoke_serp.json, "dumps", counting_dumps)

    result = smoke_serp.main(
        ["--api-key", "synthetic-key", "--query", "query"],
        service_factory=lambda key: service,
    )

    output = capsys.readouterr().out.strip()
    assert result == 0
    assert service.calls[1][1] is engine
    search_parameters = service.calls[2][1]
    assert isinstance(search_parameters, dict)
    assert search_parameters["engine"] is engine
    assert dumps_calls <= 129
    assert len(output) <= 220


def test_smoke_prefilters_multi_megabyte_sensitive_prefixes_before_matching(
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = BoundedMatchString("a" * 2_000_000)
    api_key = "a" * 2_000_000
    query = "a" * 1_999_999 + "b"
    service = RecordingService(engine=engine, data=[])

    result = smoke_serp.main(
        ["--api-key", api_key, "--query", query], service_factory=lambda key: service
    )

    output = capsys.readouterr().out.strip()
    assert result == 0
    assert service.calls[1][1] is engine
    search_parameters = service.calls[2][1]
    assert isinstance(search_parameters, dict)
    assert search_parameters["engine"] is engine
    assert engine.comparison_lengths == []
    assert len(output) <= 220
    assert api_key not in output
    assert query not in output


def test_smoke_still_redacts_normal_sensitive_values_from_engine_summary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = "engine-key-sentinel-query-sentinel"
    service = RecordingService(engine=engine, data=[])

    result = smoke_serp.main(
        ["--api-key", "key-sentinel", "--query", "query-sentinel"],
        service_factory=lambda key: service,
    )

    output = capsys.readouterr().out.strip()
    assert result == 0
    assert output.count("[REDACTED]") == 2
    assert "key-sentinel" not in output
    assert "query-sentinel" not in output
    assert service.calls[1][1] == engine


@pytest.mark.parametrize("stage", ["list", "schema", "search"])
@pytest.mark.parametrize(
    "error",
    [
        SchemaError("failure synthetic-key"),
        ParameterError("failure synthetic-key"),
        ResponseError("failure synthetic-key"),
        SerpApiError(401, "failure synthetic-key"),
        requests.RequestException("failure synthetic-key"),
    ],
)
def test_smoke_expected_failures_are_safe_and_stop_the_sequence(
    capsys: pytest.CaptureFixture[str], stage: str, error: Exception
) -> None:
    service = RecordingService(fail_at=stage, error=error)

    result = smoke_serp.main(["--api-key", "synthetic-key"], service_factory=lambda key: service)

    output = capsys.readouterr().out.strip()
    assert result == 1
    assert output == "ok=false error=failure [REDACTED]"
    assert "synthetic-key" not in output
    assert [call[0] for call in service.calls] == ["list", "schema", "search"][
        : ["list", "schema", "search"].index(stage) + 1
    ]


@pytest.mark.parametrize("error_type", [ValueError, TypeError, RuntimeError])
def test_smoke_unexpected_failures_hide_details_and_exception_chain(
    capsys: pytest.CaptureFixture[str], error_type: type[Exception]
) -> None:
    secret = "synthetic-key"
    try:
        raise RuntimeError(f"inner detail {secret}")
    except RuntimeError as cause:
        try:
            raise error_type(f"outer detail {secret}") from cause
        except error_type as error:
            chained_error = error
    service = RecordingService(fail_at="list", error=chained_error)

    result = smoke_serp.main(["--api-key", secret], service_factory=lambda key: service)

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out.strip() == "ok=false error=Unable to complete Thordata SERP smoke test."
    assert captured.err == ""
    assert secret not in captured.out
    assert "inner detail" not in captured.out
    assert "outer detail" not in captured.out


def test_smoke_unexpected_broken_error_string_uses_fixed_generic_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    service = RecordingService(fail_at="list", error=BrokenStringError())

    result = smoke_serp.main(["--api-key", "synthetic-key"], service_factory=lambda key: service)

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out.strip() == "ok=false error=Unable to complete Thordata SERP smoke test."
    assert captured.err == ""
    assert "synthetic-key" not in captured.out


def test_smoke_unexpected_failure_uses_generic_secret_free_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "synthetic-key"

    result = smoke_serp.main(
        ["--api-key", secret],
        service_factory=lambda key: (_ for _ in ()).throw(RuntimeError(f"boom {key}")),
    )

    output = capsys.readouterr().out.strip()
    assert result == 1
    assert output == "ok=false error=Unable to complete Thordata SERP smoke test."
    assert secret not in output
