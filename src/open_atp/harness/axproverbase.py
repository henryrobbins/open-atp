"""ax-prover-base harness (LangGraph Lean agent driven by ``ax-prover prove``)."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any, ClassVar

from open_atp.auth import AuthStatus
from open_atp.harness._paths import _SCRIPTS
from open_atp.harness.base import Harness, HarnessRunResult

log = logging.getLogger("open_atp")

#: Cap on ax-prover's per-call LLM retries (its ``DEFAULT_LLM_RETRY_CONFIG`` ships
#: ``stop_after_attempt=10000`` -- ~8h20m). That ``with_retry`` fires on *any* exception
#: and ignores Anthropic's ``x-should-retry: false``, so a non-retryable ``400`` (e.g. a
#: bad request from a model/langchain-anthropic mismatch) gets retried 10k times and the
#: run silently hangs for hours instead of failing fast. Three is plenty to ride out a
#: transient blip while turning any hard error into a prompt, visible failure.
_LLM_MAX_RETRIES = 3


def _infer_provider(model: str) -> str:
    if model.startswith("claude"):
        return "anthropic"
    if model.startswith("deepseek"):
        return "deepseek"
    if model.startswith("gemini"):
        return "google"
    return "openai"


class AxProverBaseHarness(Harness):
    """ax-prover-base (LangGraph Lean agent), driven by ``ax-prover prove`` in-sandbox.

    ax-prover is a self-contained proving agent (its own
    proposer->builder->reviewer->memory loop) that edits the target ``.lean`` file
    in place. It slots in as a harness rather than a standalone prover because
    :meth:`AgentProver.prove` already supplies everything around the edges (staging,
    snapshot/diff, sandbox run, key forwarding) and the shared ``Verifier`` -- not
    ax-prover's own reviewer -- remains the source of truth for compile/sorry/axiom.

    Two things differ from the CLI harnesses (it mirrors :class:`VibeHarness` here):

    * **Config lives in a workdir YAML, not flags.** :meth:`stage_wd` writes
      ``axprover.yaml`` selecting the model/effort/iterations; it layers on top of
      ax-prover's bundled ``default.yaml`` (auto-prepended by the CLI), so it only
      needs to override the deltas.
    * **Cost is not on stdout.** ax-prover streams human-readable logs, so token
      usage is read from the JSON it writes per target instead. ``cost_usd`` is left
      ``None`` and the prover converts tokens->USD via the fallback table, exactly
      like :class:`CodexHarness`.

    Parameters
    ----------
    model : str, default "claude-opus-4-8"
        Model id the agent runs, mapped to ax-prover's ``provider:model`` string.
    effort : str, default "high"
        Reasoning-effort level, mapped to each provider's knob.
    max_iterations : int, optional
        Cap on ax-prover's proposer->builder->reviewer loop. ``None`` (default) keeps
        ax-prover's own default (50).
    provider_api_key : str, optional
        The selected provider's API key, forwarded under its canonical env var
        (``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY`` / ...). ``None`` (default) reads
        that env var from the host; resolution fails if neither is set.

    Examples
    --------

    Constructing the harness resolves its defaults:

    >>> from open_atp.harness import AxProverBaseHarness
    >>> harness = AxProverBaseHarness()
    >>> harness.name
    'axproverbase'
    >>> harness.model
    'claude-opus-4-8'

    With the provider key supplied explicitly, :meth:`agent_auth` forwards it under
    the provider's canonical env var without reading the host environment:

    >>> harness = AxProverBaseHarness(provider_api_key="sk-fake")
    >>> harness.agent_auth().env
    {'ANTHROPIC_API_KEY': 'sk-fake'}
    """

    name = "axproverbase"

    #: open-atp provider name -> ax-prover's LangChain ``provider:model`` prefix.
    _AX_PROVIDER_PREFIX: ClassVar[dict[str, str]] = {
        "anthropic": "anthropic",
        "openai": "openai",
        "google": "google_genai",
        "deepseek": "deepseek",
    }

    #: open-atp provider name -> the canonical env var ax-prover reads its key from.
    _PROVIDER_ENV: ClassVar[dict[str, str]] = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "google": "GOOGLE_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
    }

    def __init__(
        self,
        *,
        model: str = "claude-opus-4-8",
        effort: str = "high",
        max_iterations: int | None = None,
        provider_api_key: str | None = None,
    ) -> None:
        super().__init__(model=model, effort=effort)
        self.max_iterations = max_iterations
        self._provider_api_key = provider_api_key

    def auth_status(self) -> AuthStatus:
        env_name = self._PROVIDER_ENV[_infer_provider(self.model)]
        return AuthStatus.from_env(
            env_name,
            self._provider_api_key,
            remedy=f"set {env_name} for {self.model}",
        )

    def _required_env(self) -> dict[str, str]:
        # ax-prover reads the provider key from the process env; forward the selected
        # provider's key under its canonical name (ANTHROPIC_API_KEY / OPENAI_API_KEY
        # / GOOGLE_API_KEY / DEEPSEEK_API_KEY).
        env_name = self._PROVIDER_ENV[_infer_provider(self.model)]
        return self._key_env(env_name, self._provider_api_key)

    def stage_wd(self, wd: Path) -> None:
        # ax-prover has its own prompts and ignores the written prompt, but the base
        # launch contract still cats it, so one is still written.
        super().stage_wd(wd)
        (wd / "axprover.yaml").write_text(self._render_config())

    def _ax_model(self) -> str:
        """``self.model`` as ax-prover's ``provider:model`` string."""
        provider = _infer_provider(self.model)
        prefix = self._AX_PROVIDER_PREFIX.get(provider, "openai")
        return f"{prefix}:{self.model}"

    def _provider_config(self) -> dict[str, Any]:
        """Provider-specific LLM kwargs mapping ``effort`` to each API's knob."""
        provider = _infer_provider(self.model)
        if provider == "anthropic":
            return {
                "temperature": 1.0,  # required when thinking is enabled
                "max_tokens": None,
                "effort": self.effort,
                "thinking": {"type": "adaptive"},
            }
        if provider == "google":
            return {
                "temperature": 1.0,
                "max_tokens": None,
                "include_thoughts": True,
                "thinking_level": self.effort,
            }
        return {  # openai / deepseek (OpenAI-compatible)
            "temperature": None,
            "max_tokens": None,
            "reasoning": {"effort": self.effort},
        }

    def _render_config(self) -> str:
        """The ``axprover.yaml`` overrides layered over ax-prover's ``default.yaml``.

        JSON is valid YAML, so we emit JSON to avoid a YAML dependency. Only the
        deltas are set: ``prover_llm`` (which the bundled config's ``memory_config``
        and ``summarize_output`` interpolate from) and, when capped, ``max_iterations``.
        The bundled ``proposer_tools`` (lean + web search) are left untouched -- a
        missing TAVILY_API_KEY or blocked egress degrades a tool to a no-op rather
        than failing the run.

        The LLM is defined under a *fresh* ``llm_configs.open_atp`` key and
        ``prover_llm`` points at it via interpolation, rather than inlining the dict.
        ax-prover's ``--config`` argparse flag *appends* to its ``["default.yaml"]``
        default (it does not replace it), so default.yaml's
        ``prover_llm: ${llm_configs.claude_opus_4_5}`` is always in the merge. An
        inline ``prover_llm`` dict would then OmegaConf-*deep-merge* onto that resolved
        config and silently inherit stale keys -- notably ``thinking.budget_tokens:
        10000`` from ``claude_opus_4_5``'s ``thinking.type: enabled``, which the API
        rejects ("Extra inputs are not permitted") under our ``thinking.type: adaptive``
        and which sends every request into a retry storm (no usage, just 400s). A
        brand-new key has nothing to merge with, so our provider config is used
        verbatim; ``${llm_configs.open_atp}`` (a string node) cleanly *replaces*
        default.yaml's interpolation, and ``llm_configs`` is stripped after resolution
        as a non-Config temporary key.

        ``retry_config`` overrides only ``stop_after_attempt`` (see
        :data:`_LLM_MAX_RETRIES`); merge_configs' final merge onto the structured
        ``LLMConfig`` schema deep-merges it over ax-prover's
        ``DEFAULT_LLM_RETRY_CONFIG``, so the exponential-jitter wait is preserved --
        we just refuse to retry 10k times.
        """
        config: dict[str, Any] = {
            "llm_configs": {
                "open_atp": {
                    "model": self._ax_model(),
                    "provider_config": self._provider_config(),
                    "retry_config": {"stop_after_attempt": _LLM_MAX_RETRIES},
                }
            },
            "prover": {"prover_llm": "${llm_configs.open_atp}"},
        }
        if self.max_iterations is not None:
            config["prover"]["max_iterations"] = int(self.max_iterations)
        return json.dumps(config, indent=2)

    def _agent_command(self) -> str:
        # No <<MODEL>>/<<EFFORT>> substitution: those live in axprover.yaml.
        return (_SCRIPTS / "axprover_agent.sh").read_text()

    def parse_result(self, lines: list[str], wd: Path) -> HarnessRunResult:
        # Tokens come from the per-target ``-o`` files (ax_output.<target>.json) under
        # ``wd``, each a ``{location: {success, ..., input_tokens, output_tokens}}`` map
        # written by the launch script. Sum across every target in every file; the
        # stream itself carries no usage. Leave cost_usd None so the prover derives USD
        # from the token table (like CodexHarness).
        result = self._parse_lines(lines)
        output_files = sorted(wd.glob("ax_output.*.json")) if wd.is_dir() else []
        if not output_files:
            log.warning(
                "no ax-prover output files found; tokens/cost unavailable",
                extra={"harness": self.name, "wd": str(wd)},
            )
        for path in output_files:
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                log.warning(
                    "unreadable ax-prover output file",
                    extra={"harness": self.name, "path": str(path)},
                )
                continue
            entries = data.values() if isinstance(data, dict) else []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                result.input_tokens += int(entry.get("input_tokens", 0) or 0)
                result.output_tokens += int(entry.get("output_tokens", 0) or 0)
        self._log_usage(result)
        return result

    def collect_logs(self, wd: Path, logs_dir: Path) -> None:
        # ax-prover's rich record is the per-target ``ax_output.<target>.json`` (usage
        # + outcome) and the teed ``ax_prover.<target>.log`` (human-readable run log),
        # both written into the workdir. Move them out to ``logs/`` --
        # ``parse_result`` has already summed the token fields, so relocating is safe.
        moved = sorted(wd.glob("ax_output.*.json")) + sorted(wd.glob("ax_prover.*.log"))
        if moved:
            logs_dir.mkdir(parents=True, exist_ok=True)
            for path in moved:
                shutil.move(str(path), str(logs_dir / path.name))

    def _parse_lines(self, lines: list[str]) -> HarnessRunResult:
        # ax-prover's stdout is human-readable logs, not a JSON event stream; keep the
        # last non-empty line as result text for debugging and read tokens elsewhere.
        result = HarnessRunResult()
        for line in lines:
            stripped = line.strip()
            if stripped:
                result.result_text = stripped
        return result
