"""KiminaProver tests.

Generation (``_generate``) is stubbed with canned candidates, so the splice / pass@k
selection / statement-guard logic runs fully offline -- no GPU, model, or network.
The pure splice helpers are tested directly; the selection tests swap in a fake
verifier so they need no Docker. One Docker-marked end-to-end exercises the *real*
shared verifier over a canned proof (mocked generation, real local check) -- the path
every prover shares.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType

import pytest

from open_afps.backends.docker import DockerBackend, DockerConfig
from open_afps.core.result import VerificationReport
from open_afps.core.task import LeanProject, ProofTask
from open_afps.images import DEFAULT_IMAGE, DEFAULT_TOOLCHAIN
from open_afps.provers._lean_splice import extract_theorems, splice_proof
from open_afps.provers.kimina import KiminaProver, KiminaProverConfig

FIXTURE = Path(__file__).parent / "fixtures" / "mil_trivial"

GOOD_BODY = "\n  rw [mul_comm a b, mul_assoc b a c]"
BAD_BODY = "\n  BROKEN_TACTIC"
# A body that smuggles a column-0 redefinition of the target under a weaker statement;
# the statement guard must reject it even though it carries no `sorry`.
MALICIOUS_BODY = (
    "\n  rw [mul_comm a b, mul_assoc b a c]"
    "\n\ntheorem mul_comm_assoc (a b : ℝ) : a = a := by rfl"
)


# --- pure helpers: extract_theorems / splice_proof --------------------------


def test_extract_theorems_finds_sorry_target() -> None:
    text = (FIXTURE / "MILExample.lean").read_text()
    thms = extract_theorems(text)

    assert len(thms) == 1
    th = thms[0]
    assert th.name == "mul_comm_assoc"
    assert th.statement.endswith(":= by")
    assert "a * b * c = b * (a * c)" in th.statement
    # The body span covers the proof after `by` (the `sorry`), not the signature.
    start, end = th.body_span
    assert text[start:end].strip() == "sorry"


def test_extract_theorems_skips_already_proved_and_handles_multiple() -> None:
    text = (
        "import Mathlib\n\n"
        "theorem done : 1 = 1 := by rfl\n\n"
        "theorem todo (n : ℕ) : n = n := by\n  sorry\n\n"
        "lemma also_todo : True := by sorry\n"
    )
    thms = extract_theorems(text)

    names = [t.name for t in thms]
    assert names == ["todo", "also_todo"]  # `done` is already proved -> skipped


def test_extract_theorems_ignores_assign_inside_signature() -> None:
    # A `:=` in a default-valued binder must not be mistaken for the proof delimiter.
    text = "theorem t (h : Nat := by exact 0) : True := by\n  sorry\n"
    (th,) = extract_theorems(text)

    assert th.statement.endswith(":= by")
    assert "True" in th.statement
    assert text[th.body_span[0] : th.body_span[1]].strip() == "sorry"


def test_extract_theorems_captures_preceding_docstring() -> None:
    text = (FIXTURE / "MILExample.lean").read_text()
    (th,) = extract_theorems(text)

    # The fixture's `/-! ... -/` comment becomes informal-problem context.
    assert "trivial exercise" in th.docstring.lower()


def test_docstring_empty_when_no_comment() -> None:
    (th,) = extract_theorems("theorem t : True := by\n  sorry\n")
    assert th.docstring == ""


def test_splice_proof_replaces_only_the_body() -> None:
    text = (FIXTURE / "MILExample.lean").read_text()
    (th,) = extract_theorems(text)

    spliced = splice_proof(text, th.body_span, GOOD_BODY)

    # The proof `sorry` is gone (the word still appears in the file's doc comment).
    assert not extract_theorems(spliced)
    assert "rw [mul_comm a b, mul_assoc b a c]" in spliced
    # The signature survives untouched.
    assert "theorem mul_comm_assoc (a b c : ℝ)" in spliced


# --- selection / guard with a fake verifier (offline) -----------------------


class _FakeVerifier:
    """Stand-in for the shared verifier: a file "compiles" unless it says BROKEN."""

    def verify(self, project: LeanProject) -> VerificationReport:
        per_file = {
            p.relative_to(project.root).as_posix(): "BROKEN" not in p.read_text()
            for p in project.lean_files()
        }
        return VerificationReport(
            compiles=all(per_file.values()),
            sorry_free=True,
            per_file=per_file,
        )


def _make_prover(candidates: dict[str, list[str]]) -> KiminaProver:
    backend = DockerBackend(DockerConfig(image=DEFAULT_IMAGE))
    config = KiminaProverConfig(
        image=DEFAULT_IMAGE, supported_toolchain=DEFAULT_TOOLCHAIN, pass_k=4
    )
    prover = KiminaProver(config, backend)
    prover._generate = lambda statements, workdir: candidates  # type: ignore[assignment]
    prover.verifier = _FakeVerifier()  # type: ignore[assignment]
    return prover


def test_passk_selection_skips_failing_candidate(tmp_path: Path) -> None:
    """A non-compiling first candidate is skipped; the next verifying one wins."""
    prover = _make_prover({"mul_comm_assoc": [BAD_BODY, GOOD_BODY]})
    workdir = tmp_path / "wd"

    output = prover.prove(ProofTask(LeanProject(FIXTURE)), workdir)

    assert output.metadata["winning_index"] == {"MILExample.lean:mul_comm_assoc": 1}
    assert output.metadata["samples_tried"] == 2
    assert "MILExample.lean" in output.completed_files
    final = (workdir / "MILExample.lean").read_text()
    assert not extract_theorems(final)  # the proof `sorry` was filled
    assert "rw [mul_comm a b, mul_assoc b a c]" in final


def test_no_verifying_candidate_leaves_original(tmp_path: Path) -> None:
    """When nothing verifies, the file is left on the original (still sorry'd)."""
    prover = _make_prover({"mul_comm_assoc": [BAD_BODY]})
    workdir = tmp_path / "wd"

    output = prover.prove(ProofTask(LeanProject(FIXTURE)), workdir)

    assert output.metadata["winning_index"] == {"MILExample.lean:mul_comm_assoc": None}
    assert output.completed_files == {}  # unchanged from the staged original
    # The target is still unproved (a sorry'd theorem remains).
    assert extract_theorems((workdir / "MILExample.lean").read_text())


def test_statement_guard_rejects_signature_change(tmp_path: Path) -> None:
    """A candidate that smuggles a weakened restatement is rejected by the guard."""
    prover = _make_prover({"mul_comm_assoc": [MALICIOUS_BODY]})
    workdir = tmp_path / "wd"

    output = prover.prove(ProofTask(LeanProject(FIXTURE)), workdir)

    assert output.metadata["winning_index"] == {"MILExample.lean:mul_comm_assoc": None}
    # Rejected and reverted: the target is back to its sorry'd original.
    assert extract_theorems((workdir / "MILExample.lean").read_text())


def test_guard_disabled_accepts_signature_change(tmp_path: Path) -> None:
    """With the guard off, the same candidate is accepted (compiles per fake)."""
    backend = DockerBackend(DockerConfig(image=DEFAULT_IMAGE))
    config = KiminaProverConfig(
        image=DEFAULT_IMAGE,
        supported_toolchain=DEFAULT_TOOLCHAIN,
        guard_statements=False,
    )
    prover = KiminaProver(config, backend)
    prover._generate = lambda statements, workdir: {  # type: ignore[assignment]
        "mul_comm_assoc": [MALICIOUS_BODY]
    }
    prover.verifier = _FakeVerifier()  # type: ignore[assignment]

    output = prover.prove(ProofTask(LeanProject(FIXTURE)), tmp_path / "wd")

    assert output.metadata["winning_index"] == {"MILExample.lean:mul_comm_assoc": 0}


def test_problem_context_threaded_from_docstring(tmp_path: Path) -> None:
    """The target's doc comment is forwarded to generation as informal context."""
    prover = _make_prover({"mul_comm_assoc": [GOOD_BODY]})
    captured: dict[str, object] = {}

    def fake_gen(statements: object, workdir: Path) -> dict[str, list[str]]:
        captured["statements"] = statements
        return {"mul_comm_assoc": [GOOD_BODY]}

    prover._generate = fake_gen  # type: ignore[assignment]

    prover.prove(ProofTask(LeanProject(FIXTURE)), tmp_path / "wd")

    payloads = captured["statements"]
    assert isinstance(payloads, list)
    assert payloads[0]["name"] == "mul_comm_assoc"
    assert "trivial exercise" in payloads[0]["problem"].lower()


def test_instructions_used_as_problem_fallback(tmp_path: Path) -> None:
    """With no doc comment, the task instructions become the informal context."""
    prover = _make_prover({"t": [GOOD_BODY]})
    captured: dict[str, object] = {}

    def fake_gen(statements: object, workdir: Path) -> dict[str, list[str]]:
        captured["statements"] = statements
        return {}

    prover._generate = fake_gen  # type: ignore[assignment]

    # Stage a project whose lone target carries no doc comment.
    proj = tmp_path / "proj"
    import shutil

    shutil.copytree(FIXTURE, proj)
    (proj / "MILExample.lean").write_text(
        "import Mathlib\n\ntheorem t : True := by\n  sorry\n"
    )

    prover.prove(
        ProofTask(LeanProject(proj), instructions="prove it is trivially true"),
        tmp_path / "wd",
    )

    payloads = captured["statements"]
    assert isinstance(payloads, list)
    assert payloads[0]["problem"] == "prove it is trivially true"


def test_model_size_registry_variants() -> None:
    """`kimina:<size>` selects a distilled checkpoint; bare `kimina` is the default."""
    from open_afps.api import available_provers, build_prover

    assert {"kimina", "kimina:7b", "kimina:1.5b"} <= set(available_provers())

    backend = DockerBackend(DockerConfig(image=DEFAULT_IMAGE))
    small = build_prover(
        "kimina:1.5b",
        image=DEFAULT_IMAGE,
        toolchain=DEFAULT_TOOLCHAIN,
        verification_backend=backend,
    )
    assert "1.5B" in small.config.model  # type: ignore[attr-defined]


def test_generate_without_backend_raises(tmp_path: Path) -> None:
    """The real generation seam errors clearly when no GPU backend is wired."""
    backend = DockerBackend(DockerConfig(image=DEFAULT_IMAGE))
    config = KiminaProverConfig(
        image=DEFAULT_IMAGE, supported_toolchain=DEFAULT_TOOLCHAIN
    )
    prover = KiminaProver(config, backend)  # no generation backend

    with pytest.raises(RuntimeError, match="GPU generation backend"):
        prover._generate(
            [{"name": "t", "statement": "theorem t : True := by"}], tmp_path
        )


# --- end-to-end with the real shared verifier (Docker) ----------------------


@pytest.mark.docker
def test_run_end_to_end_verifies_winning_candidate(tmp_path: Path) -> None:
    """Full run(): stubbed generation, real Docker verifier confirms the proof."""
    backend = DockerBackend(DockerConfig(image=DEFAULT_IMAGE))
    config = KiminaProverConfig(
        image=DEFAULT_IMAGE, supported_toolchain=DEFAULT_TOOLCHAIN
    )
    prover = KiminaProver(config, backend)
    # First candidate is a real tactic that fails to close the goal; second is the
    # genuine proof -- so selection must skip the first and land on the second.
    prover._generate = lambda statements, workdir: {  # type: ignore[assignment]
        "mul_comm_assoc": ["\n  rfl", GOOD_BODY]
    }

    result = prover.run(ProofTask(LeanProject(FIXTURE)), tmp_path / "wd")

    assert result.success, result.verification and result.verification.compile_log
    assert result.verification is not None and result.verification.verified
    assert result.prover == "kimina"
    assert result.cost_usd is None  # self-served on GPU; no per-run dollar cost


# --- generation entrypoint helpers (images/kimina/kimina_generate.py) --------
#
# Loaded by path: it ships in the GPU image, not the package. Its vLLM/transformers
# imports are deferred into generate(), so the prompt/extraction helpers import with
# no GPU or those deps.

_GEN_PATH = (
    Path(__file__).resolve().parents[1] / "images" / "kimina" / "kimina_generate.py"
)


def _load_gen() -> ModuleType:
    spec = importlib.util.spec_from_file_location("kimina_generate", _GEN_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_build_prompt_follows_model_card_recipe() -> None:
    gen = _load_gen()
    prompt = gen.build_prompt("theorem t : True := by", problem="show True")

    assert "step by step in Lean 4" in prompt
    assert "# Problem:show True" in prompt
    assert "```lean4\n" in prompt and "theorem t : True := by" in prompt
    # A statement without an import is given Mathlib context.
    assert "import Mathlib" in prompt


def test_build_prompt_keeps_existing_imports() -> None:
    gen = _load_gen()
    prompt = gen.build_prompt("import Mathlib\n\ntheorem t : True := by")

    assert prompt.count("import Mathlib") == 1


def test_extract_proof_body_takes_last_complete_block() -> None:
    gen = _load_gen()
    output = (
        "Here is my reasoning...\n"
        "```lean4\ntheorem t : True := by\n  sorry\n```\n"
        "Wait, let me finish it:\n"
        "```lean4\ntheorem t : True := by\n  trivial\n```\n"
    )
    assert gen.extract_proof_body(output) == "\n  trivial"


def test_extract_proof_body_handles_inline_and_missing() -> None:
    gen = _load_gen()
    assert gen.extract_proof_body("```lean\ntheorem t : True := by rfl\n```") == " rfl"
    assert gen.extract_proof_body("no code here at all") is None
    # A block with no proof delimiter yields nothing usable.
    assert gen.extract_proof_body("```lean\n#check Nat\n```") is None


def test_extract_proof_body_anchors_on_final_answer() -> None:
    """The real model shape: a <think> trace with draft fences, then the answer.

    The reasoning's draft proof (`simp`) and a stray fence must NOT be picked up; only
    the answer after the last </think> counts.
    """
    gen = _load_gen()
    output = (
        "<think>\nDraft:\n```lean\ntheorem foo := by simp\n```\nstray ```\noddly.\n"
        "</think>\n"
        "```lean4\nimport Mathlib\n\n"
        "theorem mul_comm_assoc (a b c : ℝ) : a * b * c = b * (a * c) := by\n"
        "  ring\n```\n"
    )
    assert gen.extract_proof_body(output) == "\n  ring"


# --- real generation end-to-end on a Modal GPU (slow, opt-in) ---------------


def _modal_configured() -> bool:
    if os.environ.get("MODAL_TOKEN_ID") and os.environ.get("MODAL_TOKEN_SECRET"):
        return True
    return (Path.home() / ".modal.toml").is_file()


@pytest.mark.slow
@pytest.mark.modal
@pytest.mark.skipif(
    not _modal_configured(),
    reason="Modal not configured; also needs the published `kimina` GPU image",
)
def test_real_generation_verifies_on_modal_gpu(tmp_path: Path) -> None:
    """Real Kimina generation on a Modal GPU -> the shared verifier confirms it.

    Requires Modal credentials and the image published via `open-afps
    build-kimina-image`. Verify runs on the same image (no GPU); generation gets a
    GPU and an HF-cache volume so weights persist across runs.
    """
    from open_afps.backends.modal import ModalBackend, ModalConfig

    verify = ModalBackend(ModalConfig(image="kimina"))
    generate = ModalBackend(
        ModalConfig(
            image="kimina",
            gpu="A100",
            timeout_s=3600,
            volumes={"kimina-hf-cache": "/root/.cache/huggingface"},
            warm_lean=False,  # vLLM generation needs no Lean build
        )
    )
    config = KiminaProverConfig(
        image="kimina", supported_toolchain=DEFAULT_TOOLCHAIN, pass_k=8
    )
    prover = KiminaProver(config, verify, generate)

    result = prover.run(ProofTask(LeanProject(FIXTURE)), tmp_path / "wd")

    assert result.success, result.verification and result.verification.compile_log
    assert result.verification is not None and result.verification.verified
    assert result.metadata["samples_tried"] >= 1
