"""ax-prover-base harness (LangGraph Lean agent driven by ``ax-prover prove``)."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, ClassVar

from open_afps.harness._paths import _SCRIPTS
from open_afps.harness.base import AuthSpec, Harness, HarnessRunResult, _infer_provider
from open_afps.harness.bundles import AssetBundle

#: Cap on ax-prover's per-call LLM retries (its ``DEFAULT_LLM_RETRY_CONFIG`` ships
#: ``stop_after_attempt=10000`` -- ~8h20m). That ``with_retry`` fires on *any* exception
#: and ignores Anthropic's ``x-should-retry: false``, so a non-retryable ``400`` (e.g. a
#: bad request from a model/langchain-anthropic mismatch) gets retried 10k times and the
#: run silently hangs for hours instead of failing fast. Three is plenty to ride out a
#: transient blip while turning any hard error into a prompt, visible failure.
_LLM_MAX_RETRIES = 3


class AxProverHarness(Harness):
    """ax-prover-base (LangGraph Lean agent), driven by ``ax-prover prove`` in-sandbox.

    ax-prover is a self-contained proving agent (its own
    proposer->builder->reviewer->memory loop) that edits the target ``.lean`` file
    in place. It slots in as a harness rather than a standalone prover because
    :meth:`AgentProver.prove` already supplies everything around the edges (staging,
    snapshot/diff, sandbox run, key forwarding) and the shared ``Verifier`` -- not
    ax-prover's own reviewer -- remains the source of truth for compile/sorry/axiom.

    Two things differ from the CLI harnesses (it mirrors :class:`VibeHarness` here):

    * **Config lives in a workdir YAML, not flags.** :meth:`configure_wd` writes
      ``axprover.yaml`` selecting the model/effort/iterations; it layers on top of
      ax-prover's bundled ``default.yaml`` (auto-prepended by the CLI), so it only
      needs to override the deltas.
    * **Cost is not on stdout.** ax-prover streams human-readable logs; token usage
      comes from its ``-o`` JSON (the per-target ``ax_output.<target>.json`` files the
      launch script writes), which carries ``input_tokens``/``output_tokens`` alongside
      ``{success, error, summary}`` as of the pinned fork commit (see ``AX_PROVER_REF``
      in ``__main__.py``). :meth:`parse` sums those across every target and leaves
      ``cost_usd`` ``None`` so the prover converts tokens->USD via the fallback table,
      exactly like :class:`CodexHarness`. (On an ax-prover build without those fields
      the tokens are simply absent and the run reports zero cost.)
    """

    name = "axprover"

    #: open-afps provider name -> ax-prover's LangChain ``provider:model`` prefix.
    _AX_PROVIDER_PREFIX: ClassVar[dict[str, str]] = {
        "anthropic": "anthropic",
        "openai": "openai",
        "google": "google_genai",
        "deepseek": "deepseek",
    }

    def __init__(
        self,
        model: str,
        effort: str = "medium",
        *,
        max_iterations: int | None = None,
        assets: AssetBundle | None = None,
    ) -> None:
        super().__init__(model, effort, assets)
        self.max_iterations = max_iterations
        #: Set in :meth:`configure_wd`; where :meth:`parse` looks for usage files.
        self._wd: Path | None = None

    @classmethod
    def from_config(cls, config: Any, *, assets: AssetBundle | None = None) -> Harness:
        return cls(
            config.model,
            config.effort,
            max_iterations=getattr(config, "max_iterations", None),
            assets=assets,
        )

    def auth_spec(self) -> AuthSpec:
        # Raw provider keys, exactly like OpenCodeHarness; ax-prover reads them from
        # the process env (ANTHROPIC_API_KEY / OPENAI_API_KEY / GOOGLE_API_KEY).
        env = [
            key
            for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY")
            if key in os.environ
        ]
        if not env:
            raise RuntimeError(
                "axprover harness requires one of ANTHROPIC_API_KEY / "
                "OPENAI_API_KEY / GOOGLE_API_KEY"
            )
        return AuthSpec(env=env)

    def configure_wd(self, wd: Path, prompt: str) -> None:
        # The free-text prompt is ignored: ax-prover has its own prompts. We still let
        # the base write agent.sh + agent_prompt.txt for a uniform contract.
        super().configure_wd(wd, prompt)
        (wd / "axprover.yaml").write_text(self._render_config())
        self._wd = wd

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

        The LLM is defined under a *fresh* ``llm_configs.open_afps`` key and
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
        verbatim; ``${llm_configs.open_afps}`` (a string node) cleanly *replaces*
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
                "open_afps": {
                    "model": self._ax_model(),
                    "provider_config": self._provider_config(),
                    "retry_config": {"stop_after_attempt": _LLM_MAX_RETRIES},
                }
            },
            "prover": {"prover_llm": "${llm_configs.open_afps}"},
        }
        if self.max_iterations is not None:
            config["prover"]["max_iterations"] = int(self.max_iterations)
        return json.dumps(config, indent=2)

    def _agent_command(self) -> str:
        # No <<MODEL>>/<<EFFORT>> substitution: those live in axprover.yaml.
        return (_SCRIPTS / "axprover_agent.sh").read_text()

    def parse(self, lines: list[str]) -> HarnessRunResult:
        # Tokens come from the per-target ``-o`` files (ax_output.<target>.json), each
        # a ``{location: {success, ..., input_tokens, output_tokens, ...}}`` map written
        # by the launch script. Sum across every target in every file; the stream itself
        # carries no usage. Leave cost_usd None so the prover derives USD from the token
        # table (like CodexHarness).
        result = self._parse_lines(lines)
        if self._wd is not None and self._wd.is_dir():
            for path in sorted(self._wd.glob("ax_output.*.json")):
                try:
                    data = json.loads(path.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                entries = data.values() if isinstance(data, dict) else []
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    result.input_tokens += int(entry.get("input_tokens", 0) or 0)
                    result.output_tokens += int(entry.get("output_tokens", 0) or 0)
        return result

    def collect_logs(self, wd: Path, logs_dir: Path) -> None:
        # ax-prover's rich record is the per-target ``ax_output.<target>.json`` (usage
        # + outcome) and the teed ``ax_prover.<target>.log`` (human-readable run log),
        # both written into the workdir. Move them out to ``logs/`` -- ``parse`` has
        # already summed the token fields, so relocating is safe.
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
