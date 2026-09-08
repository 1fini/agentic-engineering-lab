"""Bounded subprocess transport used by ARGUS worker adapters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import os
import signal
import subprocess
import tempfile
import time
from typing import Mapping


class ProcessOutcome(StrEnum):
    COMPLETED = "completed"
    PROCESS_ERROR = "process_error"
    TIMEOUT = "timeout"
    OUTPUT_LIMIT = "output_limit"
    TERMINATION_UNCERTAIN = "termination_uncertain"


@dataclass(frozen=True)
class ProcessResult:
    outcome: ProcessOutcome
    duration_ms: int
    exit_code: int | None
    stdout: bytes
    stderr_excerpt: str | None
    detail: str | None = None


@dataclass(frozen=True)
class BoundedProcessConfig:
    command: tuple[str, ...]
    cwd: str | None = None
    timeout_seconds: float = 600.0
    termination_grace_seconds: float = 1.0
    max_stdout_bytes: int = 1_048_576
    max_stderr_bytes: int = 65_536
    env: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if not self.command or not self.command[0]:
            raise ValueError("command must be non-empty")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        if self.termination_grace_seconds <= 0:
            raise ValueError("termination_grace_seconds must be > 0")
        if self.max_stdout_bytes < 1 or self.max_stderr_bytes < 1:
            raise ValueError("output limits must be positive")


class BoundedProcess:
    """Execute one subprocess with timeout and bounded retained output.

    stdout/stderr are redirected to temporary files so a noisy worker cannot force
    unbounded in-memory buffering. Output larger than configured limits is rejected.
    """

    def __init__(self, config: BoundedProcessConfig) -> None:
        self.config = config

    def run(self, stdin_data: bytes | None = None) -> ProcessResult:
        started = time.monotonic()
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            try:
                process = subprocess.Popen(
                    list(self.config.command),
                    stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    cwd=self.config.cwd,
                    env=dict(self.config.env) if self.config.env is not None else None,
                    start_new_session=(os.name == "posix"),
                    creationflags=_windows_creation_flags(),
                )
            except OSError as exc:
                return ProcessResult(
                    outcome=ProcessOutcome.PROCESS_ERROR,
                    duration_ms=_elapsed_ms(started),
                    exit_code=None,
                    stdout=b"",
                    stderr_excerpt=None,
                    detail=f"failed to start worker process: {exc.__class__.__name__}",
                )

            timed_out = False
            termination_proven = True
            try:
                process.communicate(input=stdin_data, timeout=self.config.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                termination_proven = _terminate_process_tree(
                    process,
                    grace_seconds=self.config.termination_grace_seconds,
                )

            duration_ms = _elapsed_ms(started)
            stdout_size = _file_size(stdout_file)
            stderr_size = _file_size(stderr_file)
            stdout = _read_prefix(stdout_file, self.config.max_stdout_bytes)
            stderr_bytes = _read_prefix(stderr_file, self.config.max_stderr_bytes)
            stderr_excerpt = _decode_excerpt(stderr_bytes) if stderr_bytes else None

            if timed_out and not termination_proven:
                return ProcessResult(
                    outcome=ProcessOutcome.TERMINATION_UNCERTAIN,
                    duration_ms=duration_ms,
                    exit_code=process.poll(),
                    stdout=stdout,
                    stderr_excerpt=stderr_excerpt,
                    detail="worker timed out and process-tree termination could not be proven",
                )
            if timed_out:
                return ProcessResult(
                    outcome=ProcessOutcome.TIMEOUT,
                    duration_ms=duration_ms,
                    exit_code=process.poll(),
                    stdout=stdout,
                    stderr_excerpt=stderr_excerpt,
                    detail="worker exceeded timeout and was terminated",
                )
            if stdout_size > self.config.max_stdout_bytes or stderr_size > self.config.max_stderr_bytes:
                return ProcessResult(
                    outcome=ProcessOutcome.OUTPUT_LIMIT,
                    duration_ms=duration_ms,
                    exit_code=process.returncode,
                    stdout=stdout,
                    stderr_excerpt=stderr_excerpt,
                    detail=(
                        "worker output exceeded configured limit "
                        f"(stdout={stdout_size}, stderr={stderr_size})"
                    ),
                )
            if process.returncode != 0:
                return ProcessResult(
                    outcome=ProcessOutcome.PROCESS_ERROR,
                    duration_ms=duration_ms,
                    exit_code=process.returncode,
                    stdout=stdout,
                    stderr_excerpt=stderr_excerpt,
                    detail=f"worker process exited with code {process.returncode}",
                )
            return ProcessResult(
                outcome=ProcessOutcome.COMPLETED,
                duration_ms=duration_ms,
                exit_code=process.returncode,
                stdout=stdout,
                stderr_excerpt=stderr_excerpt,
            )


def _terminate_process_tree(process: subprocess.Popen[bytes], *, grace_seconds: float) -> bool:
    if process.poll() is not None:
        return True

    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        try:
            process.wait(timeout=grace_seconds)
            return True
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return True
            except OSError:
                return False
            try:
                process.wait(timeout=grace_seconds)
                return True
            except subprocess.TimeoutExpired:
                return False

    # The standard library cannot prove descendant termination portably on Windows.
    # Fail closed at the transport boundary instead of claiming tree termination.
    try:
        process.terminate()
        process.wait(timeout=grace_seconds)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=grace_seconds)
        except (OSError, subprocess.TimeoutExpired):
            return False
    return False


def _windows_creation_flags() -> int:
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


def _file_size(file_obj) -> int:
    file_obj.flush()
    return os.fstat(file_obj.fileno()).st_size


def _read_prefix(file_obj, limit: int) -> bytes:
    file_obj.flush()
    file_obj.seek(0)
    return file_obj.read(limit)


def _decode_excerpt(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))
