"""The harness contract: the *agent* concern of :class:`~...agent_prover.AgentProver`.

A :class:`Harness` knows, for one agent CLI (Claude Code / Codex / OpenCode):

* how to populate the working directory (launch script, MCP config, skills,
  prompt) -- :meth:`Harness.configure_wd`;
* the bash command that launches the agent -- :attr:`Harness.command`;
* which credentials to forward into the sandbox -- :meth:`Harness.auth_spec`; and
* how to read token/cost totals out of the agent's streamed JSON
  -- :meth:`Harness.parse`.

The *compute* concern (where that command runs, with Lean+Mathlib and a warm
cache) lives in the injected :class:`~open_afps.backends.base.ComputeBackend`.

Ported from milp_flare's ``harness/`` package; skills/plugins to mount are
carried by the injected :class:`~open_afps.harness.bundles.AssetBundle` (the
default mounts the vendored ``lean-proof`` skill, plus the ``lean4`` plugin for
the Claude harness).
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from open_afps.harness.bundles import DEFAULT_BUNDLE, AssetBundle

#: Files the harness writes into the workdir; named so they never collide with a
#: project's own sources.
SCRIPT_FILE = "agent.sh"
PROMPT_FILE = "agent_prompt.txt"


@dataclass(frozen=True)
class AuthSpec:
    """Compute-agnostic description of the credentials a harness needs.

    Attributes
    ----------
    env:
        Host environment-variable names to forward into the sandbox.
    home_dirs:
        Host directories to expose under the sandbox's ``$HOME``, as
        ``(host_dir, dest_basename)`` pairs (e.g. ``(~/.codex, ".codex")``).
    """

    env: list[str] = field(default_factory=list)
    home_dirs: list[tuple[Path, str]] = field(default_factory=list)


@dataclass
class HarnessRunResult:
    """Token totals and cost parsed from an agent's streamed output."""

    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str | None = None
    #: USD cost if the harness self-reports it (Claude Code, OpenCode); ``None``
    #: when it must be estimated from token counts (Codex).
    cost_usd: float | None = None
    #: Final ``type:"result"`` subtype (Claude Code: ``success`` /
    #: ``error_max_turns`` / ``error_during_execution``). Used by NuminaProver's
    #: round loop to decide continue-vs-stop when no END_REASON marker is present.
    subtype: str | None = None
    #: The agent's final result text (Claude Code's ``result`` field), where the
    #: Numina coordinator prints its ``END_REASON:<reason>`` marker.
    result_text: str | None = None


class Harness(ABC):
    """Base class for an agent CLI harness."""

    name: ClassVar[str]

    def __init__(
        self, model: str, effort: str = "medium", assets: AssetBundle | None = None
    ) -> None:
        self.model = model
        self.effort = effort
        self.assets = assets or DEFAULT_BUNDLE

    @classmethod
    def from_config(cls, config: Any, *, assets: AssetBundle | None = None) -> Harness:
        """Build a harness from an :class:`AgentProverConfig`.

        The default reads only the shared ``model``/``effort`` knobs; harnesses with
        extra config (e.g. :class:`VibeHarness`'s agent/turn/price) override this.
        """
        return cls(config.model, config.effort, assets=assets)

    @property
    def command(self) -> str:
        """Bash command the backend runs to launch the agent.

        The backend has already ``cd``'d into the workdir and symlinked ``.lake``;
        we export ``$PROMPT`` from the written prompt file (the launch scripts
        reference it) and run the rendered script.
        """
        return f'export PROMPT="$(cat {PROMPT_FILE})" && bash {SCRIPT_FILE}'

    def static_env(self) -> dict[str, str]:
        """Non-secret env vars to set for this harness (e.g. ``IS_SANDBOX``)."""
        return {}

    def auth_spec(self) -> AuthSpec:
        """Credentials to forward into the sandbox for this harness."""
        return AuthSpec()

    def configure_wd(self, wd: Path, prompt: str) -> None:
        """Populate ``wd`` with the launch script, prompt, MCP config, and skills."""
        if not wd.exists():
            raise RuntimeError("The agent working directory must be created first.")
        (wd / SCRIPT_FILE).write_text(self._agent_command())
        (wd / PROMPT_FILE).write_text(prompt)
        self._copy_extra_dirs(wd)

    def _copy_extra_dirs(self, wd: Path) -> None:
        """Copy the bundle's extra asset trees (e.g. Numina's prompts) into ``wd``."""
        for src, dest in self.assets.extra_dirs:
            target = wd / dest
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, target, dirs_exist_ok=True)

    def parse(self, lines: list[str]) -> HarnessRunResult:
        """Parse the agent's streamed JSON lines into a :class:`HarnessRunResult`."""
        return self._parse_lines(lines)

    def collect_logs(self, wd: Path, logs_dir: Path) -> None:
        """Move this harness's rich log files out of ``wd`` into ``logs_dir``.

        The streamed event JSONL the prover captures from stdout *is* the agent's
        transcript for every CLI harness, so the default does nothing. Harnesses that
        *also* drop a richer record inside the workdir (Vibe's session log, ax-prover's
        per-target logs) override this to relocate those files -- so ``download_wd``
        stays the proof project and ``download_logs`` carries the full record. Called
        after :meth:`parse` (which may read those files for cost), so moving them is
        safe.
        """

    def _copy_skills(self, wd: Path, dest: str) -> None:
        """Copy the selected bundle's skills into ``wd/<dest>``.

        Two mount modes (a bundle may use either or both):

        * each dir in ``skills`` -> ``wd/<dest>/<dir-name>/`` (ordinary named
          skills; an upstream ``tests/`` fixture dir is dropped); and
        * the legacy ``skills_dir`` -> its *contents* to ``wd/<dest>/`` (a single
          root-mounted skill bundle, e.g. Numina).

        A no-op when the bundle mounts no skills.
        """
        if self.assets.skills_dir is None and not self.assets.skills:
            return
        target = wd / dest
        target.mkdir(parents=True, exist_ok=True)
        if self.assets.skills_dir is not None:
            shutil.copytree(self.assets.skills_dir, target, dirs_exist_ok=True)
        for skill in self.assets.skills:
            shutil.copytree(
                skill,
                target / skill.name,
                ignore=shutil.ignore_patterns("tests"),
                dirs_exist_ok=True,
            )

    def _render(self, template: str) -> str:
        """Substitute ``<<MODEL>>``/``<<EFFORT>>`` into a launch-script template."""
        return template.replace("<<MODEL>>", self.model).replace(
            "<<EFFORT>>", self.effort
        )

    @abstractmethod
    def _agent_command(self) -> str:
        """The rendered contents of the workdir's ``agent.sh``."""

    @abstractmethod
    def _parse_lines(self, lines: list[str]) -> HarnessRunResult: ...


def _infer_provider(model: str) -> str:
    if model.startswith("claude"):
        return "anthropic"
    if model.startswith("deepseek"):
        return "deepseek"
    if model.startswith("gemini"):
        return "google"
    return "openai"
