from __future__ import annotations

import os

import pytest

from argus.opencode_adapter import OpenCodeConfig, OpenCodeWorkerAdapter
from argus.worker_adapter import InvocationOutcome
from argus.worker_protocol import WorkerRequest


def test_opencode_command_requires_explicit_model_and_keeps_payload_out_of_argv(tmp_path) -> None:
    with pytest.raises(ValueError, match="model"):
        OpenCodeConfig(model="")

    adapter = OpenCodeWorkerAdapter(
        OpenCodeConfig(
            model="provider/model",
            executable="opencode",
            working_directory=str(tmp_path),
            agent="worker",
            variant="high",
        )
    )
    command = adapter.build_command(tmp_path / "request.json")
    assert command[:4] == ("opencode", "run", "--model", "provider/model")
    assert "--agent" in command
    assert "--variant" in command
    assert "--dir" in command
    assert "--file" in command
    assert str(tmp_path / "request.json") in command
    assert "secret-value" not in " ".join(command)
    prompt_index = next(index for index, value in enumerate(command) if value.startswith("ARGUS bounded worker protocol v1."))
    assert command.index("--file") > prompt_index


def test_protocol_critical_opencode_flags_cannot_be_overridden() -> None:
    for arg in ("--format", "--file", "--model", "--thinking"):
        with pytest.raises(ValueError, match="protocol-critical"):
            OpenCodeConfig(model="provider/model", extra_args=(arg, "anything"))


@pytest.mark.skipif(os.name == "nt", reason="fake executable fixture uses a POSIX shebang")
def test_opencode_adapter_invokes_fake_cli_with_private_request_attachment(tmp_path) -> None:
    fake = tmp_path / "fake-opencode"
    fake.write_text(
        """#!/usr/bin/env python3
import json, pathlib, sys
args = sys.argv[1:]
request_path = pathlib.Path(args[args.index('--file') + 1])
request = json.loads(request_path.read_text(encoding='utf-8'))
print(json.dumps({
    'schema_version': 1,
    'request_id': request['request_id'],
    'outcome': 'success',
    'output': {'operation': request['operation']},
    'message': None,
    'retry_after_seconds': None,
    'usage': {'input_tokens': 10, 'output_tokens': 4, 'cost_usd': 0.001},
}))
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)

    request = WorkerRequest(
        request_id="req-1",
        operation="fixture.work",
        payload={"secret": "secret-value"},
    )
    adapter = OpenCodeWorkerAdapter(
        OpenCodeConfig(
            model="provider/model",
            executable=str(fake),
            working_directory=str(tmp_path),
            timeout_seconds=2,
        )
    )
    result = adapter.invoke(request)
    assert result.outcome is InvocationOutcome.SUCCESS
    assert result.response is not None
    assert result.response.output == {"operation": "fixture.work"}
    assert result.response.usage is not None
    assert result.response.usage.input_tokens == 10


@pytest.mark.skipif(os.name == "nt", reason="fake executable fixture uses a POSIX shebang")
def test_opencode_adapter_rejects_markdown_wrapped_response(tmp_path) -> None:
    fake = tmp_path / "fake-opencode"
    fake.write_text(
        """#!/usr/bin/env python3
print('```json')
print('{\"schema_version\":1,\"request_id\":\"req-1\",\"outcome\":\"success\",\"output\":{}}')
print('```')
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)

    result = OpenCodeWorkerAdapter(
        OpenCodeConfig(
            model="provider/model",
            executable=str(fake),
            working_directory=str(tmp_path),
            timeout_seconds=2,
        )
    ).invoke(WorkerRequest(request_id="req-1", operation="fixture.work", payload={}))
    assert result.outcome is InvocationOutcome.MALFORMED_OUTPUT
