"""``open-atp`` CLI: a thin shell over the prover API.

    open-atp prove <path> <output> <prover>

The core stays a plain Python API (:func:`open_atp.standard_prover` ->
:meth:`~open_atp.provers.base.AutomatedProver.prove`); this is just the terminal
front door, deliberately minimal: pick a registered prover, point at a lake project
(or a single bare ``.lean`` file, staged into the pinned skeleton), and choose where
the ``{wd,logs}`` output lands. Generation + verification run on the chosen compute
backend (``--compute docker``/``modal``) with the default image/toolchain.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import TextIO, cast

import structlog
import yaml
from rich.box import ROUNDED
from rich.console import Console
from rich.table import Table
from structlog.typing import Processor
from tqdm import tqdm

from open_atp.auth import AuthState, AuthStatus
from open_atp.backends import _BACKENDS
from open_atp.backends.base import ComputeBackend
from open_atp.benchmark import (
    DATASET,
    BenchmarkResult,
    download_dataset,
    run_benchmark,
    tasks_from_dir,
)
from open_atp.config import (
    _build_backend,
    _build_prover,
    standard_prover,
    standard_provers,
)
from open_atp.images import DEFAULT_IMAGE
from open_atp.lean import LeanProject, ProofTask, create_project
from open_atp.provers.base import AutomatedProver, ProofResult, ProofStatus

#: ax-prover baked into the Modal image (mirrors the images/Dockerfile ARG). Pinned
#: to a commit on our fork (henryrobbins/ax-prover-base) rather than the 0.1.1 PyPI
#: release, for two fixes the release lacks:
#:   * lean_interact-based target discovery -- 0.1.1's regex discovery lists
#:     ``import Mathlib`` as a theorem ``Mathlib`` and flags it "unproven" whenever a
#:     nearby docstring contains the word ``sorry``, wasting a prove loop on a phantom
#:     target; the rewrite asks the Lean server for real declarations + ``Sorry`` terms.
#:   * per-target token usage in the ``-o`` JSON, which
#:     ``AxProverBaseHarness.parse_result`` reads to report cost (the PyPI release emits
#:     no usage).
#: Fork is public; HTTPS clone needs no credentials in the image build.
AX_PROVER_REPO = "https://github.com/henryrobbins/ax-prover-base"
AX_PROVER_REF = "361e5b3451267785bfd70f173e7ab0be667d4987"
AX_PROVER_SPEC = f"git+{AX_PROVER_REPO}@{AX_PROVER_REF}"


class _TqdmStream:
    """A file-like sink that routes log lines through :meth:`tqdm.write`.

    Keeps log output from clobbering an active progress bar.
    """

    def write(self, message: str) -> int:
        tqdm.write(message, end="")
        return len(message)

    def flush(self) -> None:
        pass


#: ``--log-level`` choices mapped to their :mod:`logging` levels.
_LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def _configure_logging(console_level: int, log_file: Path | None = None) -> None:
    """Render the ``open_atp`` logger's records with up to two sinks.

    open-atp is a well-behaved library: every module logs to the plain stdlib
    ``open_atp`` logger and configures nothing itself. As the application, the CLI owns
    that logger here -- it attaches the handlers, sets the level, and turns off
    propagation so the output stays isolated (a rude third-party library that hijacks
    the root logger, e.g. ``aristotlelib``, can neither duplicate nor swallow our logs).

    :mod:`structlog` appears only as a formatter, never as a global config: a
    ``ProcessorFormatter`` renders each stdlib record through structlog's processors, so
    ``extra={...}`` fields and exception tracebacks come out structured. The console
    keeps the pretty ``ConsoleRenderer`` (via the tqdm-aware stream) at the console
    level with compact ``HH:MM:SS`` timestamps. When ``log_file`` is given, a handler
    writes full-detail JSONL there (one event per line) at ``DEBUG`` regardless of the
    console level, with ISO-8601 timestamps, so a quiet terminal never costs you the
    audit log.
    """

    def formatter(
        render: list[Processor], timestamp_fmt: str
    ) -> structlog.stdlib.ProcessorFormatter:
        # Every open_atp record is a plain stdlib record ("foreign" to structlog), so
        # the pre-chain rebuilds the event dict: merge the ``extra={...}`` fields and
        # the level in before the shared timestamp/render tail runs.
        return structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=[
                structlog.contextvars.merge_contextvars,
                structlog.stdlib.ExtraAdder(),
                structlog.stdlib.add_log_level,
            ],
            processors=[
                structlog.processors.TimeStamper(fmt=timestamp_fmt),
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                *render,
            ],
        )

    console = logging.StreamHandler(cast("TextIO", _TqdmStream()))
    console.setFormatter(formatter([structlog.dev.ConsoleRenderer()], "%H:%M:%S"))
    console.setLevel(console_level)
    handlers: list[logging.Handler] = [console]

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(
            formatter(
                [
                    structlog.processors.format_exc_info,
                    structlog.processors.JSONRenderer(),
                ],
                "iso",
            )
        )
        file_handler.setLevel(logging.DEBUG)
        handlers.append(file_handler)

    # Own the open_atp logger directly (not root): the handlers hang here, the logger
    # passes everything the sinks might want (DEBUG when a file sink is present), and
    # propagate=False keeps our records off the root logger entirely.
    logger = logging.getLogger("open_atp")
    logger.handlers = handlers
    logger.setLevel(logging.DEBUG if log_file is not None else console_level)
    logger.propagate = False


def _load_dotenv() -> None:
    """Load credentials from a ``.env`` (``KEY=VALUE`` per line) for the CLI.

    Searches the cwd and its parents for the first ``.env`` and seeds any missing
    environment variables (e.g. ``ARISTOTLE_API_KEY``) so the provers find their
    credentials. Real environment variables already set are left untouched.
    """
    cwd = Path.cwd()
    for directory in (cwd, *cwd.parents):
        env_file = directory / ".env"
        if not env_file.is_file():
            continue
        for raw in env_file.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
        return


def _check(ok: bool) -> str:
    """A green ✓ / red ✗ rich markup cell for a boolean."""
    return "[green]✓[/]" if ok else "[red]✗[/]"


def _proof_table(result: ProofResult) -> Table:
    """A two-column ``field``/``value`` table summarizing a :class:`ProofResult`."""
    if result.status is ProofStatus.VERIFIED:
        status = "[green]✓ verified[/]"
    elif result.status is ProofStatus.UNVERIFIED:
        status = "[red]✗ unverified[/]"
    else:
        status = f"[red]{result.status.value}[/]"
        if result.error:
            status += f": {result.error}"

    table = Table(box=ROUNDED, show_header=False)
    table.add_column("field", style="bold")
    table.add_column("value")
    table.add_row("status", status)
    table.add_row("prover", result.prover)
    cost = f"${result.cost_usd:.4f}" if result.cost_usd is not None else "—"
    time = f"{result.duration_s:.0f}s" if result.duration_s is not None else "—"
    table.add_row("cost", cost)
    table.add_row("time", time)
    table.add_row("output", str(result.output_dir))
    if result.verification is not None:
        v = result.verification
        table.add_row("compiles", _check(v.compiles))
        table.add_row("sorry-free", _check(v.sorry_free))
        bad = v.non_standard_axioms
        table.add_row("axioms", f"[red]✗ {', '.join(bad)}[/]" if bad else _check(True))
    return table


def _benchmark_table(result: BenchmarkResult) -> Table:
    """A table with one ``(task, prover)`` row per cell: status, cost, time."""
    table = Table(box=ROUNDED)
    table.add_column("task")
    table.add_column("prover")
    table.add_column("status", justify="center")
    table.add_column("cost", justify="right")
    table.add_column("time", justify="right")
    for run in result.runs:
        r = run.result
        if r.status is ProofStatus.VERIFIED:
            status = "[green]✓[/]"
        elif r.status is ProofStatus.UNVERIFIED:
            status = "[red]✗[/]"
        else:
            status = f"[red]{r.status.value}[/]"
        cost = f"${r.cost_usd:.4f}" if r.cost_usd is not None else "—"
        time = f"{r.duration_s:.0f}s" if r.duration_s is not None else "—"
        table.add_row(run.task, run.prover, status, cost, time)
    return table


#: Row style per credential state: valid is green, nearly-expired yellow, and both
#: "won't authenticate" states red.
_AUTH_STYLES = {
    AuthState.OK: "green",
    AuthState.EXPIRING: "yellow",
    AuthState.EXPIRED: "red",
    AuthState.MISSING: "red",
}


def _format_remaining(remaining: timedelta | None) -> str:
    """A credential's time left, coarsened to its two largest units.

    Blank once the window has passed -- the status column already says it expired,
    and how long ago is not what you act on.
    """
    if remaining is None or remaining <= timedelta(0):
        return "—"
    hours, seconds = divmod(int(remaining.total_seconds()), 3600)
    days, hours = divmod(hours, 24)
    minutes = seconds // 60
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def _abbreviate_home(source: str) -> str:
    """A credential path shortened against ``$HOME``; env var names pass through."""
    home = str(Path.home())
    return f"~{source[len(home) :]}" if source.startswith(home) else source


def _auth_table(statuses: Mapping[str, AuthStatus]) -> Table:
    """A per-prover credential table: kind, where it lives, and how long it lasts."""
    table = Table(box=ROUNDED)
    table.add_column("prover")
    table.add_column("auth")
    # Fold rather than truncate: a narrow terminal must not hide which env var or
    # file to go fix.
    table.add_column("credential", overflow="fold")
    table.add_column("status", justify="center")
    table.add_column("expires in", justify="right")
    for name, status in statuses.items():
        state = status.state()
        style = _AUTH_STYLES[state]
        table.add_row(
            name,
            status.kind.value,
            _abbreviate_home(status.source),
            f"[{style}]{state.value}[/]",
            _format_remaining(status.time_remaining()),
        )
    return table


def _auth_status(args: argparse.Namespace) -> int:
    # Every prover needs a backend, but none is contacted here: reading a credential
    # is a host-side operation, so the default backend stands in for all of them.
    statuses = {
        name: prover.auth_status()
        for name, prover in _all_standard_provers(
            _build_backend({"type": "docker"})
        ).items()
    }

    if args.json:
        print(
            json.dumps(
                {
                    name: {
                        "kind": s.kind.value,
                        "source": s.source,
                        "present": s.present,
                        "state": s.state().value,
                        "expires_at": s.expires_at.isoformat()
                        if s.expires_at
                        else None,
                        "refreshable": s.refreshable,
                    }
                    for name, s in statuses.items()
                },
                indent=2,
            )
        )
        return 0

    Console().print(_auth_table(statuses))
    return 0


def _prove(args: argparse.Namespace) -> int:
    src = Path(args.path)
    if src.is_file() and src.suffix == ".lean":
        # A bare .lean file: stage it into the pinned skeleton so prove sees a
        # complete lake project. The completed file still lands in <output>/wd.
        project = create_project([src], Path(args.output) / "project")
    else:
        project = LeanProject(src)
    task = ProofTask(project)

    backend = _build_backend({"type": args.compute})
    prover = standard_prover(args.prover, backend=backend)
    result = prover.prove(task, Path(args.output))

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
        return 0 if result.success else 1

    Console().print(_proof_table(result))
    return 0 if result.success else 1


def _report(result: BenchmarkResult, as_json: bool) -> int:
    """Print a benchmark result (table or JSON) and return an exit code."""
    if as_json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
    else:
        Console().print(_benchmark_table(result))
        print(f"artifacts: {result.output_dir}")
    return 0 if all(run.result.success for run in result.runs) else 1


def _all_standard_provers(backend: ComputeBackend) -> dict[str, AutomatedProver]:
    return {name: standard_prover(name, backend=backend) for name in standard_provers()}


def _load_config(config_path: str | None) -> dict[str, object]:
    """Parse the ``--config`` YAML into a benchmark-settings mapping (``{}`` if none).

    Recognized keys are ``provers``, ``tasks``, ``compute``, and ``workers``; CLI flags
    override whatever the config supplies.
    """
    if config_path is None:
        return {}
    spec = yaml.safe_load(Path(config_path).read_text())
    if not isinstance(spec, dict):
        raise SystemExit("config must be a YAML mapping")
    return spec


def _build_registry(
    provers_spec: object, backend: ComputeBackend
) -> dict[str, AutomatedProver]:
    """Build the named-prover registry from a config ``provers`` value (``{}`` if none).

    The value is a single string (a standard prover name) or a list whose entries are
    each either a standard prover name or a prover-config mapping (an optional ``name``
    keys the result; otherwise it is derived from the prover/harness type). These are
    the custom provers and standard-prover overrides that ``--provers`` can reference.
    """
    if provers_spec is None:
        return {}
    entries = [provers_spec] if isinstance(provers_spec, str) else provers_spec
    if not isinstance(entries, list):
        raise SystemExit("config 'provers' must be a string or a list")

    provers: dict[str, AutomatedProver] = {}
    for i, entry in enumerate(entries):
        if isinstance(entry, str):
            name, prover = entry, standard_prover(entry, backend=backend)
        elif isinstance(entry, dict):
            name, prover = _named_prover(entry, i, backend)
        else:
            raise SystemExit(f"prover entry {i} must be a name or a config mapping")
        if name in provers:
            name = f"{name}-{i}"
        provers[name] = prover
    return provers


def _select_provers(
    registry: dict[str, AutomatedProver],
    provers_arg: str | None,
    backend: ComputeBackend,
) -> dict[str, AutomatedProver]:
    """Resolve which provers to run from a registry and an optional ``--provers`` list.

    ``--provers`` is a comma-separated list of names: each resolves to a custom prover
    or override from the config registry, else to a standard prover built by name.
    Without it, every prover in the registry is run; with an empty registry too, every
    standard prover is run.
    """
    if provers_arg is None:
        return registry or _all_standard_provers(backend)

    names = [n.strip() for n in provers_arg.split(",") if n.strip()]
    provers: dict[str, AutomatedProver] = {}
    for name in names:
        provers[name] = registry.get(name) or standard_prover(name, backend=backend)
    return provers


def _named_prover(
    entry: dict[str, object], index: int, backend: ComputeBackend
) -> tuple[str, AutomatedProver]:
    """A ``(name, prover)`` from a prover-config mapping; name derived if not given."""
    spec = dict(entry)
    name = spec.pop("name", None)
    if name is None:
        harness = spec.get("harness")
        kind = harness if isinstance(harness, str) else None
        if isinstance(harness, dict):
            kind = harness.get("type")
        if spec.get("type") == "agent" and kind:
            name = str(kind)
        else:
            name = str(spec.get("type", f"prover{index}"))
    return str(name), _build_prover(spec, backend)


def _task_filter(value: object) -> list[str] | None:
    """Normalize a ``tasks`` setting (comma string or list) to a list of names."""
    if value is None:
        return None
    items = value.split(",") if isinstance(value, str) else value
    if not isinstance(items, list):
        raise SystemExit("config 'tasks' must be a string or a list")
    return [str(t).strip() for t in items if str(t).strip()] or None


def _compute_spec(config_compute: object, cli_compute: str | None) -> dict[str, object]:
    """Resolve the compute setting + ``--compute`` flag into a backend spec.

    ``config_compute`` may be a bare type string (``"modal"``) or a full backend
    spec (``{"type": "modal", "region": "us", "cpu": 4}``). ``--compute`` overrides
    the type; if it selects a *different* backend than the config block, that block's
    backend-specific keys don't apply, so fall back to a bare spec.
    """
    if isinstance(config_compute, str):
        spec: dict[str, object] = {"type": config_compute}
    elif isinstance(config_compute, Mapping):
        spec = dict(config_compute)
    else:
        raise SystemExit("config 'compute' must be a string or a mapping")
    if cli_compute is not None and cli_compute != spec.get("type"):
        spec = {"type": cli_compute}
    return spec


def _benchmark(args: argparse.Namespace) -> int:
    """Run the configured provers over a directory of tasks and print a table.

    Settings come from the ``--config`` mapping; each CLI flag overrides its key.
    """
    config = _load_config(args.config)
    directory = Path(args.dataset)

    spec = _compute_spec(config.get("compute", "docker"), args.compute)
    backend = _build_backend(spec)
    registry = _build_registry(config.get("provers"), backend)
    provers = _select_provers(registry, args.provers, backend)
    # --timeout is the per-task generation budget: chain it onto each prover
    if args.timeout is not None:
        timeout_s = round(args.timeout * 60)
        for prover in provers.values():
            prover.timeout_s = timeout_s
    tasks = args.tasks if args.tasks is not None else config.get("tasks")
    only = _task_filter(tasks)

    workers = args.workers if args.workers is not None else config.get("workers", 1)
    if not isinstance(workers, int):
        raise SystemExit("config 'workers' must be an integer")

    result = run_benchmark(
        tasks_from_dir(directory),
        provers,
        Path(args.output),
        only=only,
        max_workers=workers,
    )
    return _report(result, args.json)


def _download(args: argparse.Namespace) -> int:
    """Download a benchmark dataset's task directory under ``dest``."""
    path = download_dataset(DATASET(args.dataset), Path(args.output))
    print(path)
    return 0


def _build_image(args: argparse.Namespace) -> int:
    images_dir = Path(__file__).resolve().parents[2] / "images"
    cmd = ["docker", "build", "-t", args.tag]
    if args.no_cache:
        cmd.append("--no-cache")
    cmd.append(str(images_dir))
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd).returncode


def _build_modal_image(args: argparse.Namespace) -> int:
    """Build and publish the sandbox image on Modal via Modal's builder methods.

    Built programmatically (rather than from images/Dockerfile) so the Modal image
    can install the Lean toolchain + tools *globally as root*: Modal ignores a
    container ``USER`` and runs everything as root, so the agent-user layout the
    Docker image uses doesn't apply. Installing globally keeps `lake`/`lean`/`uv` on
    root's PATH and -- crucially -- leaves the baked Mathlib package git repos
    root-owned, so `lake` reads them cleanly instead of re-cloning (which would wipe
    the warm cache). Kept in sync with images/Dockerfile; the two notable differences
    are exactly "global/root install" and "no ENTRYPOINT/agent user".

    Publishes a named image the ``ModalBackend`` looks up with
    ``modal.Image.from_name(name)``.
    """
    try:
        import modal
    except ModuleNotFoundError:
        print(
            "the modal compute backend requires the `modal` package; "
            "install it with `pip install open-atp`.",
            file=sys.stderr,
        )
        return 1

    lean_dir = Path(__file__).resolve().parents[2] / "images" / "lean"
    if not (lean_dir / "lakefile.toml").is_file():
        print(f"No Lean skeleton at {lean_dir}", file=sys.stderr)
        return 1

    app = modal.App.lookup(name=args.app, create_if_missing=True)
    image = (
        modal.Image.from_registry("ubuntu:24.04")
        .env({"DEBIAN_FRONTEND": "noninteractive"})
        # ripgrep is recommended for lean-lsp-mcp's local search. force_build on the
        # base layer so --force cascades through every subsequent (cached) layer.
        .run_commands(
            "apt-get update && apt-get install -y --no-install-recommends "
            "ca-certificates curl git unzip build-essential python3 python3-pip "
            "pipx ripgrep procps && rm -rf /var/lib/apt/lists/*",
            force_build=args.force,
        )
        # Node 20 + agent CLIs (Claude Code, Codex, OpenCode, Kimi Code), installed
        # globally. @moonshot-ai/kimi-code provides the `kimi` CLI the KimiHarness uses.
        .run_commands(
            "curl -fsSL https://deb.nodesource.com/setup_20.x | bash - "
            "&& apt-get install -y --no-install-recommends nodejs "
            "&& npm install -g @anthropic-ai/claude-code @openai/codex opencode-ai "
            "@moonshot-ai/kimi-code "
            "&& rm -rf /var/lib/apt/lists/*"
        )
        # elan + Lean toolchain in a global ELAN_HOME so `lake`/`lean` are on root's
        # PATH. --default-toolchain none lets images/lean/lean-toolchain pin it.
        .env({"ELAN_HOME": "/opt/elan"})
        .run_commands(
            "curl https://raw.githubusercontent.com/leanprover/elan/master/"
            "elan-init.sh -sSf | sh -s -- -y --default-toolchain none "
            "--no-modify-path"
        )
        # pipx tools (lean-lsp-mcp, uv, mistral-vibe) to global dirs so their
        # entrypoints land on PATH. mistral-vibe provides the `vibe` CLI the
        # VibeHarness drives; pinned so the builtin `lean` agent's model pin (Leanstral
        # 1.5) stays stable across rebuilds (keep in sync with images/Dockerfile).
        # Shared uv cache for the Numina skills' `uv run` deps.
        .env({"PIPX_HOME": "/opt/pipx", "PIPX_BIN_DIR": "/usr/local/bin"})
        .env({"UV_CACHE_DIR": "/opt/uv-cache"})
        .run_commands(
            "pipx install lean-lsp-mcp && pipx install uv "
            "&& pipx install mistral-vibe==2.19.0"
        )
        # ax-prover (LangGraph Lean agent) backing the AxProverBaseHarness,
        # pipx-isolated from open-atp and the CLIs. Keep AX_PROVER_REF in sync with the
        # images/Dockerfile ARG. Pinned to a git commit (not a PyPI release) for the
        # lean_interact target discovery -- see AX_PROVER_SPEC above.
        .run_commands(f"pipx install '{AX_PROVER_SPEC}'")
        # Modal's .env() sets literal values (no ${PATH} expansion like Dockerfile
        # ENV), so set an explicit PATH with /opt/elan/bin ahead of the standard dirs.
        .env(
            {
                "PATH": "/opt/elan/bin:/usr/local/sbin:/usr/local/bin:"
                "/usr/sbin:/usr/bin:/sbin:/bin"
            }
        )
        .workdir("/workspace")
        # copy=True bakes the skeleton into a build layer so the lake steps can read
        # it. lake update resolves the manifest + clones mathlib (installing the
        # pinned toolchain); cache get downloads its oleans for a warm cache.
        .add_local_dir(str(lean_dir), "/workspace", copy=True)
        .run_commands("lake update && lake exe cache get")
        # Pre-warm the uv cache with the Numina skills' PEP 723 deps (matches the
        # Docker image), so the first `uv run` in the sandbox resolves from cache.
        .run_commands(
            "printf '%s\\n' "
            "'# /// script' '# requires-python = \">=3.11\"' "
            '\'# dependencies = ["requests", "google-genai", "openai", '
            "\"anthropic\"]' '# ///' 'print(\"warmed\")' > /tmp/warm_skills.py "
            "&& uv run --no-project /tmp/warm_skills.py && rm /tmp/warm_skills.py"
        )
        # No ENTRYPOINT: the ModalBackend execs the wrapped command directly after
        # pushing the workdir.
    )
    with modal.enable_output():
        built = image.build(app)
    built.publish(args.name)

    print(f"Published Modal image {args.name!r} (app {args.app!r}).")
    print("Reference it from a Sandbox with:")
    print(f"    modal.Image.from_name({args.name!r})")
    return 0


def _add_logging_args(parser: argparse.ArgumentParser) -> None:
    """Attach the shared ``--log-level`` / ``-v`` / ``--log-file`` options."""
    parser.add_argument(
        "--log-level",
        choices=sorted(_LOG_LEVELS),
        default="info",
        help="Console log verbosity (default: info).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        "-d",
        "--debug",
        dest="verbose",
        action="store_true",
        help="Shortcut for --log-level debug.",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        dest="quiet",
        action="store_true",
        help="Shortcut for --log-level warning.",
    )


def _console_level(args: argparse.Namespace) -> int:
    """The requested console level (``-v``/``-q`` win over ``--log-level``)."""
    if args.verbose:
        return logging.DEBUG
    if args.quiet:
        return logging.WARNING
    return _LOG_LEVELS[args.log_level]


def build_parser() -> argparse.ArgumentParser:
    """The ``open-atp`` argument parser (also consumed by the docs CLI reference)."""
    parser = argparse.ArgumentParser(prog="open-atp")
    sub = parser.add_subparsers(dest="command", required=True)

    prove = sub.add_parser(
        "prove",
        help="Run a standard prover on a lake project or Lean file.",
    )
    prove.add_argument(
        "path",
        help="A lake project directory or a single .lean file.",
    )
    prove.add_argument(
        "output",
        help="Where to write the run's logs and agent working directory.",
    )
    prove.add_argument(
        "prover",
        choices=standard_provers(),
        help="Name of standard prover to run.",
    )
    prove.add_argument(
        "-c",
        "--compute",
        choices=sorted(_BACKENDS),
        default="docker",
        help="Compute backend to run generation and verification on.",
    )
    prove.add_argument(
        "--json",
        action="store_true",
        help="Emit the result as JSON.",
    )
    _add_logging_args(prove)

    benchmark = sub.add_parser(
        "benchmark",
        help="Run multiple provers over a dataset of proof tasks.",
    )
    benchmark.add_argument(
        "dataset",
        help="Directory of tasks to benchmark.",
    )
    benchmark.add_argument(
        "output",
        help="Where to write each run's logs and agent working directory.",
    )
    benchmark.add_argument(
        "--config",
        help=(
            "Path to YAML configuration for provers, tasks, compute, and workers; "
            "CLI flags override config values."
        ),
    )
    benchmark.add_argument(
        "-p",
        "--provers",
        help=(
            "Comma-separated prover names (standard provers, or names from ``--config``); "  # noqa: E501
            "run every config prover, else all standard provers, by default."
        ),
    )
    benchmark.add_argument(
        "-t",
        "--tasks",
        help="Comma-separated task names to run; every task run by default.",
    )
    benchmark.add_argument(
        "-c",
        "--compute",
        choices=sorted(_BACKENDS),
        help="Compute backend to run the sweep on (default: docker).",
    )
    benchmark.add_argument(
        "-w",
        "--workers",
        type=int,
        help="Number of workers; each worker runs a single prover on a task.",
    )
    benchmark.add_argument(
        "--timeout",
        type=float,
        help="Per-task wall-clock timeout in minutes; defaults to the prover's own.",
    )
    benchmark.add_argument(
        "--json",
        action="store_true",
        help="Emit the result as JSON.",
    )
    _add_logging_args(benchmark)

    download = sub.add_parser(
        "download", help="Download a benchmark dataset's task directory."
    )
    download.add_argument(
        "dataset",
        choices=[d.value for d in DATASET],
        help="Which dataset to download.",
    )
    download.add_argument(
        "output", help="Parent directory; the dataset lands at <output>/<dataset>."
    )

    auth = sub.add_parser(
        "auth-status",
        help="Show each standard prover's credential and how long it stays valid.",
    )
    auth.add_argument(
        "--json",
        action="store_true",
        help="Emit the statuses as JSON.",
    )

    build = sub.add_parser(
        "build-docker-image",
        help="Build the sandbox Docker image from images/Dockerfile.",
    )
    build.add_argument(
        "-t",
        "--tag",
        default=DEFAULT_IMAGE.name,
        help="Image tag.",
    )
    build.add_argument(
        "-C",
        "--no-cache",
        action="store_true",
        help="Pass --no-cache to docker build.",
    )

    build_modal = sub.add_parser(
        "build-modal-image",
        help="Build the sandbox image on Modal (from images/Dockerfile) and publish.",
    )
    build_modal.add_argument(
        "-n",
        "--name",
        default="open-atp",
        help="Name to publish the Modal image under. "
        "ModalBackend's image (sans :tag) must match this.",
    )
    build_modal.add_argument(
        "-a",
        "--app",
        default="open-atp",
        help="Modal app to associate the image build with.",
    )
    build_modal.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Force a rebuild even if Modal has cached layers.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)

    # Configure logging
    if args.command in ("prove", "benchmark"):
        console_level = _console_level(args)
        log_file: Path | None = Path(args.output) / "logs" / "open-atp.jsonl"
    else:
        console_level = logging.INFO
        log_file = None
    _configure_logging(console_level=console_level, log_file=log_file)

    if args.command == "prove":
        return _prove(args)
    if args.command == "download":
        return _download(args)
    if args.command == "benchmark":
        return _benchmark(args)
    if args.command == "auth-status":
        return _auth_status(args)
    if args.command == "build-docker-image":
        return _build_image(args)
    if args.command == "build-modal-image":
        return _build_modal_image(args)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
