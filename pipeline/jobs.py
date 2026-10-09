"""Pipeline runs: start any pipeline command from the admin or the API and run it in a background process.

A run is a row in `pipeline_runs`. Starting one spawns `manage.py run_pipeline_job <id>`, which executes the
command exactly as the CLI would and streams everything it prints into the row, so the admin and the API can show
progress while it works. Long steps (scraping, verification, sending) therefore never block a web request.
"""
import argparse
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management import call_command, load_command_class
from django.core.management.base import CommandError
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from pipeline.models import PipelineRun

COMMANDS = {
    "run_pipeline": "Full pipeline - fetch, research, decision makers, verify, write emails (never sends)",
    "fetch_businesses": "Module 1 - fetch businesses from Google Maps",
    "refresh_businesses": "Module 1 - refresh Google Maps details (category, reviews, city)",
    "research_websites": "Module 2 - research and score business websites",
    "find_stakeholders": "Module 3 - find decision makers and candidate emails",
    "verify_emails": "Module 4 - verify emails with Reacher (creates prospects)",
    "generate_emails": "Module 5 - generate outreach emails for review",
    "send_emails": "Send approved emails (preview unless --send)",
}
# Options every Django command has; they are not pipeline options.
DJANGO_OPTIONS = {"help", "version", "verbosity", "settings", "pythonpath", "traceback", "no_color", "force_color",
                  "skip_checks"}
# Options that name files or local services. They stay available on the command line but cannot be set from the
# admin or the API, so a web user can never make a run write files or read profiles at arbitrary paths.
WEB_BLOCKED_OPTIONS = {"report", "roles_file", "profile", "reacher_container", "reacher_url"}
MAX_OUTPUT_CHARS = 400_000
FLUSH_SECONDS = 2.0
QUEUED_TIMEOUT = timedelta(minutes=2)


class RunCancelled(BaseException):
    """Raised inside the run when it receives SIGTERM (Cancel in the admin/API)."""


# ---------------------------------------------------------------- command options


def command_parser(command: str) -> argparse.ArgumentParser:
    if command not in COMMANDS:
        raise ValueError(f"Unknown command '{command}'. Choose one of: {', '.join(COMMANDS)}.")
    return load_command_class("pipeline", command).create_parser("manage.py", command)


def command_actions(command: str) -> list[argparse.Action]:
    return [
        action for action in command_parser(command)._actions
        if action.dest not in DJANGO_OPTIONS | WEB_BLOCKED_OPTIONS and action.option_strings
        and action.help != argparse.SUPPRESS
    ]


def describe_command(command: str) -> dict:
    """The command's options, for the admin's run form and the API's /runs/commands/ listing."""
    options = []
    for action in command_actions(command):
        if isinstance(action, argparse._StoreTrueAction):
            kind = "flag"
        elif isinstance(action, argparse._AppendAction):
            kind = "list"
        else:
            kind = getattr(action.type, "__name__", "str") if action.type else "str"
        default = action.default if isinstance(action.default, (int, float, str, bool, type(None))) else str(action.default)
        options.append({"name": action.dest, "flag": action.option_strings[-1], "type": kind,
                        "default": default, "help": action.help or ""})
    return {"command": command, "description": COMMANDS[command], "options": options}


def build_arguments(command: str, options: dict | None = None, arguments: list | str | None = None) -> list[str]:
    """Command-line arguments from an options dict ({"limit": 5, "dry_run": true, "business_id": [1, 2]}) and/or
    raw arguments ("--limit 5" or ["--limit", "5"]). Validated with the command's own parser; raises ValueError."""
    if isinstance(arguments, str):
        try:
            args = shlex.split(arguments)
        except ValueError as error:
            raise ValueError(f"Cannot parse the arguments: {error}") from error
    else:
        args = [str(item) for item in arguments or []]
    actions = {action.dest: action for action in command_actions(command)}
    for key, value in (options or {}).items():
        action = actions.get(str(key).replace("-", "_").lstrip("_"))
        if action is None:
            raise ValueError(f"Unknown option '{key}' for {command}. Options: {', '.join(sorted(actions))}.")
        flag = action.option_strings[-1]
        if isinstance(action, argparse._StoreTrueAction):
            if value not in (True, False, None):
                raise ValueError(f"Option '{key}' is a flag; use true or false.")
            if value:
                args.append(flag)
        elif isinstance(action, argparse._AppendAction):
            for item in value if isinstance(value, (list, tuple)) else [value]:
                args += [flag, str(item)]
        elif value is not None:
            if isinstance(value, (list, dict)):
                raise ValueError(f"Option '{key}' takes a single value.")
            args += [flag, str(value)]
    parser = command_parser(command)
    try:
        parsed = parser.parse_args(args)
    except CommandError as error:
        raise ValueError(str(error).removeprefix("Error: ")) from error
    # Compare parsed values with the defaults, so abbreviations (--rep for --report) are caught too.
    for action in parser._actions:
        if action.dest in WEB_BLOCKED_OPTIONS | DJANGO_OPTIONS - {"help", "version"} and \
                getattr(parsed, action.dest, None) != action.default:
            raise ValueError(f"{action.option_strings[-1]} cannot be set from the admin or the API; "
                             "use the command line for it.")
    return args


# ---------------------------------------------------------------- starting and stopping


def start_run(command: str, arguments: list[str], user=None) -> PipelineRun:
    """Record a run and start it in the background once the surrounding transaction commits."""
    run = PipelineRun.objects.create(command=command, arguments=arguments,
                                     created_by=user if getattr(user, "is_authenticated", False) else None)
    transaction.on_commit(lambda: launch(run.pk))
    return run


def log_path(run_id: int) -> Path:
    return Path(tempfile.gettempdir()) / f"pipeline-run-{run_id}.log"


def launch(run_id: int) -> None:
    with open(log_path(run_id), "ab") as log:
        subprocess.Popen(
            [sys.executable, str(Path(settings.BASE_DIR) / "manage.py"), "run_pipeline_job", str(run_id)],
            cwd=settings.BASE_DIR,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,  # survives the web server reloading; Ctrl+C on runserver does not kill it
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )


def cancel_run(run: PipelineRun) -> str:
    """Ask a running run to stop (it records itself as cancelled), or cancel a run that has not started."""
    if PipelineRun.objects.filter(pk=run.pk, status=PipelineRun.Status.QUEUED).update(
        status=PipelineRun.Status.CANCELLED, finished_at=timezone.now(), output="Cancelled before it started.\n"
    ):
        return "Cancelled before it started."
    run.refresh_from_db()
    if run.status != PipelineRun.Status.RUNNING or not run.pid:
        return "This run is not running."
    if not process_alive(run.pid):
        refresh_stale_runs()
        return "The run's process had already stopped."
    os.kill(run.pid, signal.SIGTERM)
    return "Stop requested; the run will finish its current step and record itself as cancelled."


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    cmdline = Path(f"/proc/{pid}/cmdline")
    if cmdline.exists():  # the pid may have been reused by another program
        try:
            return b"run_pipeline_job" in cmdline.read_bytes()
        except OSError:
            return True
    return True


def refresh_stale_runs() -> None:
    """Mark runs failed whose process died without recording a result (e.g. the machine restarted)."""
    now = timezone.now()
    for run in PipelineRun.objects.filter(status=PipelineRun.Status.RUNNING).exclude(pid=None):
        if not process_alive(run.pid):
            PipelineRun.objects.filter(pk=run.pk, status=PipelineRun.Status.RUNNING).update(
                status=PipelineRun.Status.FAILED, finished_at=now,
                output=run.output + "\n[The process stopped without recording a result.]\n" + _log_tail(run.pk),
            )
    for run in PipelineRun.objects.filter(status=PipelineRun.Status.QUEUED, created_at__lt=now - QUEUED_TIMEOUT):
        PipelineRun.objects.filter(pk=run.pk, status=PipelineRun.Status.QUEUED).update(
            status=PipelineRun.Status.FAILED, finished_at=now,
            output="[The run never started.]\n" + _log_tail(run.pk),
        )


def _log_tail(run_id: int, chars: int = 4000) -> str:
    try:
        return log_path(run_id).read_text(errors="replace")[-chars:]
    except OSError:
        return ""


# ---------------------------------------------------------------- executing (inside the background process)


class RunOutput:
    """File-like object that collects printed output and saves it to the run every few seconds."""

    def __init__(self, run_id: int):
        self.run_id = run_id
        self.parts: list[str] = []
        self.size = 0
        self.last_save = 0.0
        self.lock = threading.Lock()

    def write(self, text: str) -> int:
        with self.lock:
            self.parts.append(text)
            self.size += len(text)
            if self.size > MAX_OUTPUT_CHARS * 2:
                self.parts = [self.text()]
                self.size = len(self.parts[0])
        if time.monotonic() - self.last_save >= FLUSH_SECONDS:
            self.save()
        return len(text)

    def flush(self) -> None:
        if time.monotonic() - self.last_save >= FLUSH_SECONDS:
            self.save()

    def isatty(self) -> bool:
        return False

    def text(self) -> str:
        joined = "".join(self.parts)
        if len(joined) > MAX_OUTPUT_CHARS:
            joined = "[earlier output trimmed]\n" + joined[-MAX_OUTPUT_CHARS:]
        return joined

    def save(self, force: bool = False) -> None:
        # Inside a transaction the write would be rolled back with it (or fail if the transaction already failed),
        # so wait until the command is outside one; the full output is rewritten each time anyway.
        if connection.in_atomic_block and not force:
            return
        self.last_save = time.monotonic()
        try:
            PipelineRun.objects.filter(pk=self.run_id).update(output=self.text())
        except DatabaseError:
            pass


def execute_run(run_id: int) -> int:
    """Run a queued pipeline run in this process and record the result. Returns the exit code."""
    if not PipelineRun.objects.filter(pk=run_id, status=PipelineRun.Status.QUEUED).update(
        status=PipelineRun.Status.RUNNING, started_at=timezone.now(), pid=os.getpid()
    ):
        return 0  # cancelled before it started, or already taken
    run = PipelineRun.objects.get(pk=run_id)
    output = RunOutput(run_id)

    def on_sigterm(signum, frame):
        raise RunCancelled()

    previous = signal.signal(signal.SIGTERM, on_sigterm)
    status, code = PipelineRun.Status.SUCCEEDED, 0
    try:
        with redirect_stdout(output), redirect_stderr(output):
            print(f"$ {run.command_line}", flush=True)
            try:
                call_command(run.command, *run.arguments)
            except CommandError as error:
                print(f"Error: {error}")
                status, code = PipelineRun.Status.FAILED, error.returncode or 1
            except RunCancelled:
                print("\nCancelled.")
                status, code = PipelineRun.Status.CANCELLED, -signal.SIGTERM
            except SystemExit as error:
                code = error.code if isinstance(error.code, int) else 1
                status = PipelineRun.Status.SUCCEEDED if code == 0 else PipelineRun.Status.FAILED
            except Exception:  # noqa: BLE001 - the traceback is the useful output
                traceback.print_exc()
                status, code = PipelineRun.Status.FAILED, 1
    except RunCancelled:  # arrived while printing the result
        status, code = PipelineRun.Status.CANCELLED, -signal.SIGTERM
    finally:
        signal.signal(signal.SIGTERM, previous)
    PipelineRun.objects.filter(pk=run_id).update(
        status=status, exit_code=code, finished_at=timezone.now(), output=output.text()
    )
    return code
