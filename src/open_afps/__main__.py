"""``open-afps`` CLI: a thin shell over :class:`~open_afps.api.Platform`.

    open-afps solve <project-dir> --provers aristotle,agent,numina [--json]

Mirrors milp_flare's arg-parsing style. The core stays a plain Python API
(:func:`open_afps.api.Platform.solve`); this is just the terminal front door.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from open_afps.api import (
    Platform,
    available_provers,
    project_from_dir,
    stage_files,
)
from open_afps.core.task import ProofTask
from open_afps.images import DEFAULT_IMAGE, DEFAULT_TOOLCHAIN

#: ax-prover PyPI version baked into the Modal image (mirrors the images/Dockerfile
#: ARG AX_PROVER_VERSION). Bump to a release that emits token usage to report cost.
AX_PROVER_VERSION = "0.1.1"


def _solve(args: argparse.Namespace) -> int:
    inputs = [Path(p) for p in args.inputs]
    if len(inputs) == 1 and inputs[0].is_dir():
        project = project_from_dir(inputs[0])
    else:
        # Bare .lean files -> stage into the pinned skeleton under the run tree.
        stage_dir = Path(args.runs_dir) / "_staged"
        project = stage_files(inputs, stage_dir)

    targets = tuple(Path(t) for t in args.targets.split(",")) if args.targets else ()
    task = ProofTask(project, targets=targets, instructions=args.instructions)

    platform = Platform(
        image=args.image,
        toolchain=args.toolchain,
        backend=args.backend,
        agent_backend=args.agent_backend,
        runs_dir=args.runs_dir,
    )
    provers = [p.strip() for p in args.provers.split(",") if p.strip()]
    result = platform.solve(task, provers, max_workers=args.max_workers)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
        return 0 if result.verified() else 1

    print(f"run {result.run_id}  ({result.run_dir})")
    for r in result.results:
        status = "✓ verified" if r.success else (r.error or "✗ unverified")
        cost = f"${r.cost_usd:.4f}" if r.cost_usd is not None else "—"
        dur = f"{r.duration_s:.0f}s" if r.duration_s is not None else "—"
        print(f"  {r.prover:<16} {status:<28} cost={cost:<10} time={dur}")
    best = result.best()
    best_name = best.prover if best else "none"
    print(f"best: {best_name}   total cost: ${result.total_cost_usd:.4f}")
    return 0 if result.verified() else 1


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
            "install it with `pip install open-afps`.",
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
        # from open-afps and the CLIs. Keep AX_PROVER_VERSION in sync with the
        # images/Dockerfile ARG; bump to a release that emits token usage so cost is
        # reported (AX_PROVER_HARNESS_PLAN.md step 3).
        .run_commands(f"pipx install 'ax-prover=={AX_PROVER_VERSION}'")
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
    parser = argparse.ArgumentParser(prog="open-afps")
    sub = parser.add_subparsers(dest="command", required=True)

    solve = sub.add_parser(
        "solve",
        help="Run provers over a lake project (or bare .lean files) and compare.",
    )
    solve.add_argument(
        "inputs",
        nargs="+",
        help="A lake project directory, or one or more bare .lean files.",
    )
    solve.add_argument(
        "--provers",
        default="agent",
        help=f"Comma-separated prover names. Available: {available_provers()}.",
    )
    solve.add_argument(
        "--instructions", default=None, help="Guidance forwarded to provers."
    )
    solve.add_argument(
        "--targets",
        default=None,
        help="Comma-separated files (relative to the project) to focus on.",
    )
    solve.add_argument("--image", default=DEFAULT_IMAGE)
    solve.add_argument("--toolchain", default=DEFAULT_TOOLCHAIN)
    solve.add_argument("--backend", default="docker", choices=["docker", "modal"])
    solve.add_argument(
        "--agent-backend",
        default=None,
        choices=["docker", "modal"],
        help="Separate generation backend (defaults to --backend).",
    )
    solve.add_argument("--runs-dir", default="runs")
    solve.add_argument("--max-workers", type=int, default=None)
    solve.add_argument(
        "--json", action="store_true", help="Emit the SolveResult as JSON."
    )

    build = sub.add_parser(
        "build-image", help="Build the sandbox Docker image from images/Dockerfile."
    )
    build.add_argument(
        "--tag", default=DEFAULT_IMAGE, help=f"Image tag (default: {DEFAULT_IMAGE})."
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
        default="open-afps",
        help="Name to publish the Modal image under (default: open-afps). "
        "ModalConfig.image (sans :tag) must match this.",
    )
    build_modal.add_argument(
        "--app",
        default="open-afps",
        help="Modal app to associate the image build with (default: open-afps).",
    )
    build_modal.add_argument(
        "--force",
        action="store_true",
        help="Force a rebuild even if Modal has cached layers.",
    )

    args = parser.parse_args(argv)
    if args.command == "solve":
        return _solve(args)
    if args.command == "build-image":
        return _build_image(args)
    if args.command == "build-modal-image":
        return _build_modal_image(args)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
