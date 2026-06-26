"""``open-atp`` CLI: a thin shell over the prover API.

    open-atp prove <prover> <lean-dir> <output-dir>

The core stays a plain Python API (:func:`open_atp.standard_prover` ->
:meth:`~open_atp.provers.base.AutomatedProver.prove`); this is just the terminal
front door, deliberately minimal: pick a registered prover, point at a lake project,
and choose where the ``{wd,logs}`` output lands. Generation + verification run on the
local Docker backend with the default image/toolchain.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TextIO, cast

import structlog
import yaml
from tqdm import tqdm

from open_atp.backends import _BACKENDS
from open_atp.backends.base import ComputeBackend
from open_atp.backends.docker import DockerBackend
from open_atp.benchmark import (
    DATASET,
    BenchmarkResult,
    download_dataset,
    run_benchmark,
    tasks_from_dir,
)
from open_atp.config import (
    _build_prover,
    build_backend,
    standard_prover,
    standard_provers,
)
from open_atp.examples import EXAMPLE, example_task
from open_atp.images import DEFAULT_IMAGE
from open_atp.lean import LeanProject, ProofTask
from open_atp.provers.base import AutomatedProver

#: ax-prover baked into the Modal image (mirrors the images/Dockerfile ARG). Pinned
#: to a commit on our fork (henryrobbins/ax-prover-base) rather than the 0.1.1 PyPI
#: release, for two fixes the release lacks:
#:   * lean_interact-based target discovery -- 0.1.1's regex discovery lists
#:     ``import Mathlib`` as a theorem ``Mathlib`` and flags it "unproven" whenever a
#:     nearby docstring contains the word ``sorry``, wasting a prove loop on a phantom
#:     target; the rewrite asks the Lean server for real declarations + ``Sorry`` terms.
#:   * per-target token usage in the ``-o`` JSON, which
#:     ``AxProverHarness.parse_result`` reads to report cost (the PyPI release emits
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


def _configure_logging() -> None:
    """Render structlog events as tidy console lines that respect the tqdm bar."""
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S"),
            structlog.dev.ConsoleRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=cast("TextIO", _TqdmStream())),
    )


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


def _prove(args: argparse.Namespace) -> int:
    project = LeanProject(Path(args.lean_dir))
    task = ProofTask(project)

    backend = DockerBackend(image=DEFAULT_IMAGE)
    prover = standard_prover(args.prover, backend=backend)
    result = prover.prove(task, Path(args.output_dir))

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
        return 0 if result.success else 1

    status = "✓ verified" if result.success else (result.error or "✗ unverified")
    cost = f"${result.cost_usd:.4f}" if result.cost_usd is not None else "—"
    dur = f"{result.duration_s:.0f}s" if result.duration_s is not None else "—"
    print(f"{result.prover:<16} {status:<28} cost={cost:<10} time={dur}")
    print(f"output: {result.output_dir}")
    return 0 if result.success else 1


def _report(result: BenchmarkResult, as_json: bool) -> int:
    """Print a benchmark result (table or JSON) and return an exit code."""
    if as_json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
    else:
        print(result.table())
        print(f"\nartifacts: {result.output_dir}")
    return 0 if all(run.result.success for run in result.runs) else 1


def _all_standard_provers(backend: ComputeBackend) -> dict[str, AutomatedProver]:
    return {name: standard_prover(name, backend=backend) for name in standard_provers()}


def _load_provers(
    directory: Path, provers_path: str | None, backend: ComputeBackend
) -> dict[str, AutomatedProver]:
    """Build the prover mapping from a YAML config, or all standard provers by default.

    Looks at ``--provers`` if given, else ``<directory>/provers.yaml`` (or ``.yml``).
    The YAML is a single string (a standard prover name) or a list whose entries are
    each either a standard prover name or a prover-config mapping (an optional ``name``
    keys the result; otherwise it is derived from the prover/harness type). When no
    config is found, every standard prover is used.
    """
    path: Path | None = Path(provers_path) if provers_path else None
    if path is None:
        candidates = (directory / n for n in ("provers.yaml", "provers.yml"))
        path = next((p for p in candidates if p.is_file()), None)
    spec = yaml.safe_load(path.read_text()) if path is not None else None
    if spec is None:
        return _all_standard_provers(backend)

    entries = [spec] if isinstance(spec, str) else spec
    if not isinstance(entries, list):
        raise SystemExit("provers config must be a string or a list")

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
            name = f"agent:{kind}"
        else:
            name = str(spec.get("type", f"prover{index}"))
    return str(name), _build_prover(spec, backend)


def _benchmark(args: argparse.Namespace) -> int:
    """Run the configured provers over a directory of tasks and print a table."""
    directory = Path(args.directory)
    backend = build_backend({"type": args.compute})
    provers = _load_provers(directory, args.provers, backend)
    tasks = tasks_from_dir(directory)
    only = (
        [t.strip() for t in args.tasks.split(",") if t.strip()] if args.tasks else None
    )
    result = run_benchmark(
        tasks,
        provers,
        Path(args.output_dir),
        only=only,
        max_workers=args.max_workers,
    )
    return _report(result, args.json)


def _download(args: argparse.Namespace) -> int:
    """Download a benchmark dataset's task directory under ``dest``."""
    path = download_dataset(DATASET(args.dataset), Path(args.dest))
    print(path)
    return 0


def _ex_benchmark(args: argparse.Namespace) -> int:
    """Run every standard prover over the five bundled examples and print a table."""
    backend = build_backend({"type": args.compute})
    tasks = {member.value: example_task(member) for member in EXAMPLE}
    result = run_benchmark(
        tasks,
        _all_standard_provers(backend),
        Path(args.output_dir),
        max_workers=args.max_workers,
    )
    return _report(result, as_json=False)


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
        # Node 20 + agent CLIs (Claude Code, Codex, OpenCode), installed globally.
        .run_commands(
            "curl -fsSL https://deb.nodesource.com/setup_20.x | bash - "
            "&& apt-get install -y --no-install-recommends nodejs "
            "&& npm install -g @anthropic-ai/claude-code @openai/codex opencode-ai "
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
        # VibeHarness drives. Shared uv cache for the Numina skills' `uv run` deps.
        .env({"PIPX_HOME": "/opt/pipx", "PIPX_BIN_DIR": "/usr/local/bin"})
        .env({"UV_CACHE_DIR": "/opt/uv-cache"})
        .run_commands(
            "pipx install lean-lsp-mcp && pipx install uv && pipx install mistral-vibe"
        )
        # ax-prover (LangGraph Lean agent) backing the AxProverHarness, pipx-isolated
        # from open-atp and the CLIs. Keep AX_PROVER_REF in sync with the
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


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    _configure_logging()
    parser = argparse.ArgumentParser(prog="open-atp")
    sub = parser.add_subparsers(dest="command", required=True)

    prove = sub.add_parser(
        "prove",
        help="Run one prover over a lake project and verify the result.",
    )
    prove.add_argument(
        "prover",
        choices=standard_provers(),
        help="Which prover to run.",
    )
    prove.add_argument("lean_dir", help="The lake project directory to complete.")
    prove.add_argument("output_dir", help="Where to write the run's {wd,logs} output.")
    prove.add_argument(
        "--json", action="store_true", help="Emit the ProofResult as JSON."
    )

    download = sub.add_parser(
        "download", help="Download a benchmark dataset's task directory."
    )
    download.add_argument(
        "dataset",
        choices=[d.value for d in DATASET],
        help="Which dataset to download.",
    )
    download.add_argument("dest", help="Directory to clone the dataset into.")

    benchmark = sub.add_parser(
        "benchmark",
        help="Run provers over a directory of tasks (e.g. a dataset); print a table.",
    )
    benchmark.add_argument(
        "directory", help="Directory of .lean tasks to benchmark (see tasks_from_dir)."
    )
    benchmark.add_argument(
        "--provers",
        help="YAML provers config (default: <directory>/provers.yaml if present, "
        "else all standard provers).",
    )
    benchmark.add_argument(
        "--tasks",
        help="Comma-separated task names to run (default: every task in the "
        "directory).",
    )
    benchmark.add_argument(
        "--compute",
        choices=sorted(_BACKENDS),
        default="docker",
        help="Compute backend to run the sweep on (default: docker).",
    )
    benchmark.add_argument(
        "--output-dir",
        default="runs/benchmark",
        help="Where to write the sweep's <task>/<prover>/ artifacts "
        "(default: runs/benchmark).",
    )
    benchmark.add_argument(
        "-n",
        "--max-workers",
        type=int,
        default=None,
        help="Max (task, prover) pairs in flight at once (default: auto; 1 = serial). "
        "Any single prover is still capped at 5 concurrent runs.",
    )
    benchmark.add_argument(
        "--json", action="store_true", help="Emit the BenchmarkResult as JSON."
    )

    ex_benchmark = sub.add_parser(
        "ex-benchmark",
        help="Run every standard prover over the 5 bundled examples; print a table.",
    )
    ex_benchmark.add_argument(
        "--compute",
        choices=sorted(_BACKENDS),
        default="docker",
        help="Compute backend to run the sweep on (default: docker).",
    )
    ex_benchmark.add_argument(
        "--output-dir",
        default="runs/ex-benchmark",
        help="Where to write the sweep's <task>/<prover>/ artifacts "
        "(default: runs/ex-benchmark).",
    )
    ex_benchmark.add_argument(
        "-n",
        "--max-workers",
        type=int,
        default=None,
        help="Max (task, prover) pairs in flight at once (default: auto; 1 = serial). "
        "Any single prover is still capped at 5 concurrent runs.",
    )

    build = sub.add_parser(
        "build-image", help="Build the sandbox Docker image from images/Dockerfile."
    )
    build.add_argument(
        "--tag",
        default=DEFAULT_IMAGE.name,
        help=f"Image tag (default: {DEFAULT_IMAGE.name}).",
    )
    build.add_argument(
        "--no-cache", action="store_true", help="Pass --no-cache to docker build."
    )

    build_modal = sub.add_parser(
        "build-modal-image",
        help="Build the sandbox image on Modal (from images/Dockerfile) and publish.",
    )
    build_modal.add_argument(
        "--name",
        default="open-atp",
        help="Name to publish the Modal image under (default: open-atp). "
        "ModalBackend's image (sans :tag) must match this.",
    )
    build_modal.add_argument(
        "--app",
        default="open-atp",
        help="Modal app to associate the image build with (default: open-atp).",
    )
    build_modal.add_argument(
        "--force",
        action="store_true",
        help="Force a rebuild even if Modal has cached layers.",
    )

    args = parser.parse_args(argv)
    if args.command == "prove":
        return _prove(args)
    if args.command == "download":
        return _download(args)
    if args.command == "benchmark":
        return _benchmark(args)
    if args.command == "ex-benchmark":
        return _ex_benchmark(args)
    if args.command == "build-image":
        return _build_image(args)
    if args.command == "build-modal-image":
        return _build_modal_image(args)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
