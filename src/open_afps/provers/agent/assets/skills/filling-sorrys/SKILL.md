---
name: filling-sorrys
description: >
  How to complete the `sorry` placeholders in a Lean 4 project so it compiles
  cleanly and is sorry-free, using the lean-lsp MCP tools to check your work.
  Use whenever the task is to fill in or finish Lean proofs in a lake project.
---

# Filling sorrys in a Lean 4 project

The working directory is a complete Lean 4 lake project. One or more `.lean`
files contain `sorry` (or `admit`) placeholders standing in for proofs that have
not been written yet. Your job is to replace every such placeholder with a real
proof so the project compiles cleanly and depends on no axioms beyond Lean's
standard set.

## Hard rules

- **Do not weaken, rename, restate, or delete any theorem, lemma, `def`,
  `structure`, or signature.** Only fill in proof bodies (the part after `:=` /
  `by` that is currently `sorry`). Changing a statement to make it easier to
  prove is failure, not success.
- **No new axioms and no `sorry`/`admit`/`native_decide`-on-false escapes.** The
  finished proof must type-check honestly. The only acceptable axioms are Lean's
  standard `propext`, `Classical.choice`, and `Quot.sound`.
- **Stay inside this working directory.** Do not read or write files outside it.
- **Do not edit** `lakefile.toml`/`lakefile.lean`, `lean-toolchain`, or
  `lake-manifest.json` — they pin the toolchain and dependencies and must match
  the verification environment.

## Workflow

1. Find the work: search for `sorry` across the `.lean` source files (e.g.
   `rg -n '\bsorry\b'`). Read each file containing one to understand the
   statement, the available hypotheses, and the relevant imports.
2. **Confirm the MCP server is live before relying on it.** Call
   `mcp__lean-lsp__lean_diagnostic_messages` on a file you have not yet edited.
   `success:true, items:[]` means it compiles cleanly; real errors come back as
   `items` with severity/message fields. `success:false, items:[]` usually means
   imports aren't built yet — run `lake build` for the relevant modules first.
3. Write a proof for one `sorry` at a time. Lean's Mathlib is available; prefer
   library lemmas, `simp`, `omega`, `linarith`, `exact?`/`apply?` suggestions,
   and `aesop` over long bespoke arguments.
4. After each edit, re-check that file with
   `mcp__lean-lsp__lean_diagnostic_messages` and iterate until it is clean.
5. When you believe a file is done, verify it has no stubbed proofs with
   `mcp__lean-lsp__lean_verify` — the reported axioms must NOT contain `sorryAx`.
6. Repeat until **no** `.lean` file contains a `sorry` and the whole project
   builds (`lake build`).

## Tips

- Use the lean-lsp tools (`mcp__lean-lsp__*`) as your primary feedback loop;
  they are far faster than a full `lake build` per change. Use `lake build`
  to materialize oleans for imports and as the final whole-project check.
- If a goal looks false or unprovable from the given hypotheses, re-read the
  statement: you likely misread a binder or a coercion. Do not "fix" it by
  changing the statement — finish the proof as stated, or, if it is genuinely
  unprovable, leave the original `sorry` and explain why rather than weakening it.
- Non-trivial proofs routinely take many rounds of compile-error fixing. Keep
  iterating against the diagnostics rather than guessing.
