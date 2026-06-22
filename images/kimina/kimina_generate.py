"""One-shot Kimina-Prover generation entrypoint, baked into the GPU image.

Reads a statements jsonl, loads ``--model`` once with vLLM, generates ``--pass-k``
candidate proofs per statement, and writes a candidates jsonl. ``KiminaProver``
invokes this via ``ComputeBackend.start`` and reads the result back -- a
command-oriented one-shot, so there is no second (server) process to manage.

The prompt format and sampling defaults are pinned to the recipe on the
``AI-MO/Kimina-Prover-Preview-Distill-7B`` model card (system prompt, the
``# Problem:`` / ``# Formal statement:`` user template, ``apply_chat_template`` with
``add_generation_prompt=True``, and ``temperature=0.6, top_p=0.95``).

I/O contract (must match ``open_afps.provers.kimina``):

* in  : ``{"name": str, "statement": str, "problem"?: str}`` per line. ``statement``
        is the formal Lean header ending in ``by``; ``problem`` is optional informal
        context.
* out : ``{"name": str, "candidates": [str, ...]}`` per line. Each candidate is a
        **proof body** -- the tactic block after ``by`` -- ready to splice over a
        ``sorry``.

The vLLM/transformers imports are deferred into :func:`generate` so the pure
proof-extraction helpers below import (and unit-test) without a GPU or those deps.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SYSTEM_PROMPT = "You are an expert in mathematics and Lean 4."

# Fenced Lean code blocks in the model's interleaved reasoning + code output.
_FENCE_RE = re.compile(r"```(?:lean4|lean)?\s*\n(.*?)```", re.DOTALL)

_OPENERS = {"(": ")", "[": "]", "{": "}"}
_CLOSERS = {")", "]", "}"}


def build_prompt(statement: str, problem: str = "") -> str:
    """Render the user prompt for one statement, per the model card recipe.

    The formal statement is given ``import Mathlib`` context if it lacks an import,
    so the model sees a self-contained block (it is trained on full statements).
    """
    formal = statement if "import " in statement else f"import Mathlib\n\n{statement}"
    prompt = "Think about and solve the following problem step by step in Lean 4."
    prompt += f"\n# Problem:{problem}"
    prompt += f"\n# Formal statement:\n```lean4\n{formal}\n```\n"
    return prompt


def _find_top_level_assign(text: str) -> int | None:
    """Index of the ``:`` in the first top-level ``:=`` (outside any bracket group)."""
    depth = 0
    for i, ch in enumerate(text):
        if ch in _OPENERS:
            depth += 1
        elif ch in _CLOSERS:
            depth -= 1
        elif ch == ":" and depth == 0 and i + 1 < len(text) and text[i + 1] == "=":
            return i
    return None


def _body_from_blocks(text: str) -> str | None:
    """Last fenced Lean block in ``text`` with a top-level ``:=`` -> its proof body."""
    blocks = [m.group(1) for m in _FENCE_RE.finditer(text)]
    for block in reversed(blocks):
        assign = _find_top_level_assign(block)
        if assign is None:
            continue
        after = block[assign + 2 :]
        by = re.match(r"\s*by\b", after)
        body = after[by.end() :] if by else after
        body = body.rstrip()
        if body:
            return body
    return None


def extract_proof_body(output_text: str) -> str | None:
    """Extract the proof body (tactic block after ``by``) from the model output.

    Returns everything after ``by`` in the final complete proof -- the portion that
    replaces a ``sorry`` -- with leading whitespace/newline preserved (so it drops in
    after a header ending in ``by``) and trailing whitespace trimmed. ``None`` if no
    usable proof is found.

    The model emits a ``<think>...</think>`` reasoning trace -- which itself contains
    draft code fences and sometimes a stray, unbalanced fence -- followed by the final
    answer. Naively pairing fences across the whole output misaligns on those drafts
    (and can grab a wrong draft proof), so we anchor on the region *after* the last
    ``</think>`` and only fall back to the whole text if that yields nothing.
    """
    if "</think>" in output_text:
        answer = output_text.rsplit("</think>", 1)[1]
        body = _body_from_blocks(answer)
        if body is not None:
            return body
    return _body_from_blocks(output_text)


def generate(
    statements: list[dict[str, str]],
    *,
    model: str,
    pass_k: int,
    temperature: float,
    top_p: float,
    max_tokens: int,
) -> list[dict[str, object]]:
    """Load the model once and return ``pass_k`` proof-body candidates per statement."""
    from transformers import AutoTokenizer  # type: ignore[import-untyped]
    from vllm import LLM, SamplingParams  # type: ignore[import-untyped]

    llm = LLM(model)
    tokenizer = AutoTokenizer.from_pretrained(model, trust_remote_code=True)
    sampling = SamplingParams(
        n=pass_k, temperature=temperature, top_p=top_p, max_tokens=max_tokens
    )

    prompts = [
        tokenizer.apply_chat_template(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": build_prompt(s["statement"], s.get("problem", "")),
                },
            ],
            tokenize=False,
            add_generation_prompt=True,
        )
        for s in statements
    ]

    results: list[dict[str, object]] = []
    for stmt, out in zip(statements, llm.generate(prompts, sampling_params=sampling)):
        name = stmt["name"]
        bodies: list[str] = []
        for i, completion in enumerate(out.outputs):
            body = extract_proof_body(completion.text)
            if body is not None:
                bodies.append(body)
                continue
            # Diagnose the miss on stderr (pulled back even if the workdir isn't):
            # finish_reason="length" means the model ran out of tokens mid-reasoning.
            finish = getattr(completion, "finish_reason", "?")
            print(
                f"[kimina] {name} sample {i}: no proof extracted "
                f"(finish={finish}, len={len(completion.text)} chars). Tail:",
                file=sys.stderr,
            )
            print(completion.text[-600:], file=sys.stderr)
        print(
            f"[kimina] {name}: {len(bodies)}/{len(out.outputs)} candidates extracted",
            file=sys.stderr,
        )
        results.append({"name": name, "candidates": bodies})
    return results


def _read_statements(path: Path) -> list[dict[str, str]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Kimina-Prover one-shot generation.")
    p.add_argument("--statements", required=True, help="Input statements jsonl.")
    p.add_argument("--out", required=True, help="Output candidates jsonl.")
    p.add_argument("--model", default="AI-MO/Kimina-Prover-Preview-Distill-7B")
    p.add_argument("--pass-k", type=int, default=32)
    p.add_argument("--temperature", type=float, default=0.6)
    p.add_argument("--top-p", type=float, default=0.95)
    p.add_argument("--max-tokens", type=int, default=8192)
    args = p.parse_args(argv)

    statements = _read_statements(Path(args.statements))
    results = generate(
        statements,
        model=args.model,
        pass_k=args.pass_k,
        temperature=args.temperature,
        top_p=args.top_p,
        max_tokens=args.max_tokens,
    )
    Path(args.out).write_text(
        "\n".join(json.dumps(r) for r in results) + "\n", encoding="utf-8"
    )
    print(f"wrote {len(results)} statement(s) to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
