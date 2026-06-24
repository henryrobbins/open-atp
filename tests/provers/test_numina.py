"""NuminaProver tests.

Three layers, mirroring the Aristotle/Agent tests:

* **Statement-tracker unit** (no Docker, no creds): port the upstream rename /
  weaken / delete detection cases, plus the crucial "filling a sorry does not count
  as a statement change" case.
* **Round-loop unit** (no Docker, no creds): stub ``_run_agent`` to emit scripted
  ``END_REASON`` sequences and assert the continue/stop/reset control flow.
* **Mocked agent + real Docker verify** (``docker`` marker): a stubbed round writes a
  real proof and the shared verifier confirms it -- no creds.

The credentialed live path (real Numina stack on the trivial fixture) lives in the
single parametrized ``test_e2e_provers.py`` suite, alongside every other prover.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_atp.backends.docker import DockerBackend, DockerConfig
from open_atp.harness import Harness
from open_atp.images import DEFAULT_IMAGE, DEFAULT_TOOLCHAIN
from open_atp.lean import LeanProject, ProofTask
from open_atp.provers.numina import NuminaProver, NuminaProverConfig
from open_atp.provers.numina_tracker import (
    StatementTracker,
    extract_statements_from_file,
    normalize_statement,
)
from open_atp.verify import ProofResult

FIXTURE = Path(__file__).parents[1] / "fixtures" / "mil_trivial"

_SRC = """\
import Mathlib

theorem foo (a b : ℝ) : a + b = b + a := by sorry

lemma bar (n : ℕ) : n + 0 = n := by sorry
"""


def _write(tmp_path: Path, text: str) -> Path:
    f = tmp_path / "T.lean"
    f.write_text(text)
    return f


# --- statement tracker -----------------------------------------------------


def test_extract_and_normalize_statements(tmp_path: Path) -> None:
    stmts = extract_statements_from_file(_write(tmp_path, _SRC))
    assert set(stmts) == {"foo", "bar"}
    assert stmts["foo"] == "theorem foo (a b : ℝ) : a + b = b + a"
    assert stmts["bar"] == "lemma bar (n : ℕ) : n + 0 = n"
    assert normalize_statement("a   +\n b") == "a + b"


def test_filling_a_sorry_is_not_a_statement_change(tmp_path: Path) -> None:
    """The whole point: completing a proof must not trip the guard."""
    f = _write(tmp_path, _SRC)
    tracker = StatementTracker([f])
    f.write_text(_SRC.replace("by sorry", "by ring", 1))
    is_valid, changes = tracker.check_initial_statements()
    assert is_valid
    assert changes == []


def test_weakened_statement_is_flagged_modified(tmp_path: Path) -> None:
    f = _write(tmp_path, _SRC)
    tracker = StatementTracker([f])
    f.write_text(_SRC.replace("a + b = b + a", "a + b = a + b"))
    changes = tracker.check()
    assert [(c.change_type, c.name) for c in changes] == [("modified", "foo")]
    assert tracker.check_initial_statements()[0] is False


def test_deleted_statement_is_flagged_removed(tmp_path: Path) -> None:
    f = _write(tmp_path, _SRC)
    tracker = StatementTracker([f])
    f.write_text(
        "import Mathlib\n\ntheorem foo (a b : ℝ) : a + b = b + a := by sorry\n"
    )
    changes = tracker.check()
    assert [(c.change_type, c.name) for c in changes] == [("removed", "bar")]
    assert tracker.check_initial_statements()[0] is False


def test_renamed_statement_is_removed_plus_added(tmp_path: Path) -> None:
    f = _write(tmp_path, _SRC)
    tracker = StatementTracker([f])
    f.write_text(_SRC.replace("theorem foo", "theorem foo'"))
    by_type = {(c.change_type, c.name) for c in tracker.check()}
    assert ("removed", "foo") in by_type
    assert ("added", "foo'") in by_type
    # The rename is rejected (a target theorem disappeared); the added one is allowed.
    is_valid, relevant = tracker.check_initial_statements()
    assert is_valid is False
    assert [(c.change_type, c.name) for c in relevant] == [("removed", "foo")]


def test_restore_puts_back_modified_and_removed(tmp_path: Path) -> None:
    f = _write(tmp_path, _SRC)
    tracker = StatementTracker([f])
    f.write_text("import Mathlib\n\ntheorem foo (a b : ℝ) : a = a := by rfl\n")
    tracker.restore_initial_statements()
    assert tracker.check_initial_statements()[0] is True


# --- round-loop control flow (no Docker) -----------------------------------


def _result_line(reason: str | None, *, subtype: str = "success") -> str:
    """One Claude stream-json result line carrying an END_REASON marker."""
    text = "work done.\n" + (f"END_REASON:{reason}" if reason else "no marker")
    return json.dumps(
        {
            "type": "result",
            "subtype": subtype,
            "result": text,
            "total_cost_usd": 0.01,
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
    )


def _make_prover(*, reuse: bool = False, **overrides: object) -> NuminaProver:
    backend = DockerBackend(DockerConfig(image=DEFAULT_IMAGE))
    config = NuminaProverConfig(
        image=DEFAULT_IMAGE, supported_toolchain=DEFAULT_TOOLCHAIN, **overrides
    )
    # reuse=True runs the whole round loop + final verify in one sandbox; reuse=False
    # (default) gives generation its own backend so the round-loop unit tests (which
    # stub _run_agent) never touch a live backend.
    agent_backend = (
        backend if reuse else DockerBackend(DockerConfig(image=DEFAULT_IMAGE))
    )
    return NuminaProver(config, backend, agent_backend)


def _run_generate(prover: NuminaProver, tmp_path: Path) -> ProofResult:
    """Drive the generation half directly (no Docker verify), returning the result.

    The public ``prove`` runs the shared verifier; the round-loop unit tests stub the
    agent and only assert on the run metadata, so they call ``_generate`` instead.
    """
    wd = tmp_path / "wd"
    logs_dir = tmp_path / "logs"
    wd.mkdir()
    logs_dir.mkdir()
    result = ProofResult(prover="numina", verification=None, output_dir=tmp_path)
    prover._generate(ProofTask(LeanProject(FIXTURE)), wd, logs_dir, result)
    return result


def _scripted_run_agent(
    reasons: list[str | None],
) -> tuple[list[int], object]:
    """A ``_run_agent`` stub that emits one scripted END_REASON per round."""
    calls: list[int] = []

    def _stub(
        self: NuminaProver,
        workdir: Path,
        harness: Harness,
        stdout_path: Path,
        session: object | None = None,
    ) -> tuple[list[str], str]:
        i = len(calls)
        calls.append(i)
        reason = reasons[i] if i < len(reasons) else reasons[-1]
        return [_result_line(reason)], ""

    return calls, _stub


def test_round_loop_continues_on_limit_then_stops_on_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, stub = _scripted_run_agent(["LIMIT", "LIMIT", "COMPLETE"])
    monkeypatch.setattr(NuminaProver, "_run_agent", stub)
    prover = _make_prover(max_rounds=20, guard_statements=False)

    out = _run_generate(prover, tmp_path)

    assert len(calls) == 3
    assert out.metadata["rounds"] == 3
    assert out.metadata["end_reason"] == "COMPLETE"
    # consecutive LIMITs hit max_consecutive_limits (2) -> one session reset.
    assert out.metadata["session_resets"] == 1
    # Cost accumulates across rounds (3 * 0.01).
    assert out.cost_usd == pytest.approx(0.03)
    assert out.metadata["input_tokens"] == 300


def test_helper_cost_is_folded_into_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """discussion_partner usage in the workdir ledger bills into cost_usd."""

    def _stub(
        self: NuminaProver,
        workdir: Path,
        harness: Harness,
        stdout_path: Path,
        session: object | None = None,
    ) -> tuple[list[str], str]:
        # Simulate the in-sandbox skill appending usage across two rounds' calls.
        ledger = workdir / ".claude" / "helper_usage.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "backend": "gpt",
                        "model": "gpt-5.4",
                        "input_tokens": 1_000_000,
                        "output_tokens": 1_000_000,
                    }
                )
                + "\n"
            )
        return [_result_line("COMPLETE")], ""

    monkeypatch.setattr(NuminaProver, "_run_agent", _stub)
    prover = _make_prover(max_rounds=20, guard_statements=False)

    out = _run_generate(prover, tmp_path)

    # gpt-5.4 = ($2.5 in + $15 out)/Mtok over 1M+1M tokens = $17.50 helper.
    assert out.metadata["agent_cost_usd"] == pytest.approx(0.01)
    assert out.metadata["helper_cost_usd"] == pytest.approx(17.5)
    assert out.cost_usd == pytest.approx(0.01 + 17.5)
    assert out.metadata["helper_breakdown"]["gpt:gpt-5.4"]["calls"] == 1
    assert out.metadata["helper_unpriced_models"] == []


def test_helper_cost_flags_unpriced_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown helper model is flagged, not silently billed at zero."""

    def _stub(
        self: NuminaProver,
        workdir: Path,
        harness: Harness,
        stdout_path: Path,
        session: object | None = None,
    ) -> tuple[list[str], str]:
        ledger = workdir / ".claude" / "helper_usage.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(
            json.dumps(
                {
                    "backend": "gemini",
                    "model": "gemini-made-up",
                    "input_tokens": 500,
                    "output_tokens": 500,
                }
            )
            + "\n"
        )
        return [_result_line("COMPLETE")], ""

    monkeypatch.setattr(NuminaProver, "_run_agent", _stub)
    prover = _make_prover(max_rounds=20, guard_statements=False)

    out = _run_generate(prover, tmp_path)

    assert out.metadata["helper_cost_usd"] == pytest.approx(0.0)
    assert out.metadata["helper_unpriced_models"] == ["gemini-made-up"]
    assert out.metadata["helper_tokens"] == {"input": 500, "output": 500}


def test_round_loop_respects_max_rounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, stub = _scripted_run_agent(["LIMIT"])  # never completes
    monkeypatch.setattr(NuminaProver, "_run_agent", stub)
    prover = _make_prover(max_rounds=4, guard_statements=False)

    out = _run_generate(prover, tmp_path)

    assert len(calls) == 4
    assert out.metadata["rounds"] == 4
    assert out.metadata["end_reason"] == "LIMIT"


def test_round_loop_stops_immediately_on_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, stub = _scripted_run_agent(["COMPLETE"])
    monkeypatch.setattr(NuminaProver, "_run_agent", stub)
    prover = _make_prover(max_rounds=20, guard_statements=False)

    out = _run_generate(prover, tmp_path)

    assert len(calls) == 1
    assert out.metadata["session_resets"] == 0


def test_round_loop_falls_back_to_subtype_when_no_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No END_REASON marker: success subtype -> COMPLETE (stop)."""
    calls: list[int] = []

    def _stub(
        self: NuminaProver,
        workdir: Path,
        harness: Harness,
        stdout_path: Path,
        session: object | None = None,
    ) -> tuple[list[str], str]:
        calls.append(1)
        return [_result_line(None, subtype="success")], ""

    monkeypatch.setattr(NuminaProver, "_run_agent", _stub)
    prover = _make_prover(max_rounds=20, guard_statements=False)

    out = _run_generate(prover, tmp_path)
    assert len(calls) == 1
    assert out.metadata["end_reason"] == "COMPLETE"


# --- statement guard inside prove() ----------------------------------------


def test_guard_error_stops_and_restores_weakened_theorem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A round that guts the theorem is rejected; the original is restored."""

    def _weaken(
        self: NuminaProver,
        workdir: Path,
        harness: Harness,
        stdout_path: Path,
        session: object | None = None,
    ) -> tuple[list[str], str]:
        target = workdir / "MILExample.lean"
        target.write_text(
            "import Mathlib\n\n"
            "theorem mul_comm_assoc (a b c : ℝ) : True := by trivial\n"
        )
        return [_result_line("COMPLETE")], ""

    monkeypatch.setattr(NuminaProver, "_run_agent", _weaken)
    prover = _make_prover(
        max_rounds=20, guard_statements=True, on_statement_change="error"
    )

    out = _run_generate(prover, tmp_path)

    assert out.metadata["statement_changed"] is True
    assert out.metadata["end_reason"] == "STATEMENT_CHANGED"
    assert out.metadata["statement_changes"]
    # The original statement was restored, so the file no longer reads `: True`.
    restored = (tmp_path / "wd" / "MILExample.lean").read_text()
    assert "a * b * c = b * (a * c)" in restored
    assert ": True" not in restored


_SOLVED_FILE = """\
import Mathlib

theorem mul_comm_assoc (a b c : ℝ) : a * b * c = b * (a * c) := by
  rw [mul_comm a b, mul_assoc b a c]
"""


# --- mocked agent + real Docker verify --------------------------------------


@pytest.mark.docker
def test_run_end_to_end_verifies_mocked_numina_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full run(): a mocked round writes a real proof; the Docker verifier confirms.

    Exercises Numina staging (vendored bundle), the round loop, the statement guard
    (the proof fills the sorry without changing the statement), and the shared
    verifier -- with no creds.
    """

    def _solve(
        self: NuminaProver,
        workdir: Path,
        harness: Harness,
        stdout_path: Path,
        session: object | None = None,
    ) -> tuple[list[str], str]:
        (workdir / "MILExample.lean").write_text(_SOLVED_FILE)
        return [_result_line("COMPLETE")], ""

    monkeypatch.setattr(NuminaProver, "_run_agent", _solve)
    prover = _make_prover(max_rounds=4)

    result = prover.prove(ProofTask(LeanProject(FIXTURE)), tmp_path / "out")

    assert result.success, result.verification and result.verification.compile_log
    assert result.verification is not None and result.verification.verified
    assert result.prover == "numina"
    assert result.metadata["end_reason"] == "COMPLETE"
    assert result.metadata["statement_changed"] is False
    assert list(result.completed_files) == ["MILExample.lean"]


@pytest.mark.docker
def test_run_reuses_one_sandbox_across_rounds_and_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """reuse=True: the whole round loop + final verify run in one live sandbox.

    Exercises ``_run_rounds`` exec'ing into the persistent session, the per-round
    ``sync_out`` feeding the host-side statement guard, and the in-session verify.
    """

    def _solve(
        self: NuminaProver,
        workdir: Path,
        harness: Harness,
        stdout_path: Path,
        session: object | None = None,
    ) -> tuple[list[str], str]:
        assert session is not None  # rounds exec in the live session
        (workdir / "MILExample.lean").write_text(_SOLVED_FILE)
        return [_result_line("COMPLETE")], ""

    monkeypatch.setattr(NuminaProver, "_run_agent", _solve)
    # Keep the session sandbox dependency-free (no real credential mounts).
    monkeypatch.setattr(NuminaProver, "_auth", lambda self, harness: ({}, []))
    prover = _make_prover(reuse=True, max_rounds=4)

    result = prover.prove(ProofTask(LeanProject(FIXTURE)), tmp_path / "out")

    assert result.success, result.verification and result.verification.compile_log
    assert result.verification is not None and result.verification.verified
    assert result.metadata["statement_changed"] is False
    assert list(result.completed_files) == ["MILExample.lean"]
