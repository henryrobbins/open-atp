"""CLI tests for the benchmark/download commands (no Docker, no network).

Covers the ``--config`` mapping parsing, the prover registry/selection (names + config
mappings, name derivation/collision, defaults to all standard provers), the task-filter
normalization, and that ``download`` dispatches to
:func:`~open_atp.benchmark.download_dataset` with the right
:class:`~open_atp.benchmark.DATASET`. Building a prover constructs it but does not run
it, so no backend is exercised.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest

from open_atp import __main__ as cli
from open_atp.config import build_backend, standard_provers


def _backend() -> object:
    return build_backend({"type": "docker"})  # constructed, never started


def _select(provers_spec: object, provers_arg: str | None) -> dict[str, object]:
    backend = _backend()
    registry = cli._build_registry(provers_spec, backend)
    return cli._select_provers(registry, provers_arg, backend)


def test_no_config_no_provers_uses_all_standard(tmp_path: Path) -> None:
    assert sorted(_select(None, None)) == sorted(standard_provers())


def test_provers_selects_standard_provers(tmp_path: Path) -> None:
    assert sorted(_select(None, "numina,aristotle")) == ["aristotle", "numina"]


def test_config_without_provers_runs_whole_config(tmp_path: Path) -> None:
    assert sorted(_select("numina", None)) == ["numina"]


def test_provers_reference_config_entries(tmp_path: Path) -> None:
    spec = [
        {"name": "my-numina", "type": "numina"},
        {
            "name": "my-codex",
            "type": "agent",
            "harness": {"type": "codex", "model": "gpt-5.5"},
        },
    ]

    provers = _select(spec, "my-codex,aristotle")

    assert sorted(provers) == ["aristotle", "my-codex"]
    assert provers["my-codex"].harness.model == "gpt-5.5"  # type: ignore[attr-defined]


def test_config_overrides_standard_prover_name(tmp_path: Path) -> None:
    spec = [
        {
            "name": "agent",
            "type": "agent",
            "harness": {"type": "codex", "model": "gpt-5.5"},
        }
    ]

    provers = _select(spec, "agent")

    assert provers["agent"].harness.model == "gpt-5.5"  # type: ignore[attr-defined]


def test_list_of_names_and_config_mappings(tmp_path: Path) -> None:
    spec = [
        "claude",
        {"type": "agent", "harness": {"type": "codex", "model": "gpt-5.5"}},
        {"name": "my-numina", "type": "numina"},
    ]

    provers = _select(spec, None)

    assert sorted(provers) == ["claude", "codex", "my-numina"]
    assert provers["codex"].harness.model == "gpt-5.5"  # type: ignore[attr-defined]


def test_duplicate_derived_names_are_suffixed(tmp_path: Path) -> None:
    provers = _select(["numina", {"type": "numina"}], None)
    assert sorted(provers) == ["numina", "numina-1"]


def test_invalid_entry_rejected(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        _select([123], None)


def test_load_config_reads_mapping(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("compute: modal\nworkers: 3\ntasks: [a, b]\nprovers: numina\n")
    config = cli._load_config(str(cfg))
    assert config == {
        "compute": "modal",
        "workers": 3,
        "tasks": ["a", "b"],
        "provers": "numina",
    }


def test_load_config_none_is_empty() -> None:
    assert cli._load_config(None) == {}


def test_load_config_rejects_non_mapping(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("- numina\n")
    with pytest.raises(SystemExit):
        cli._load_config(str(cfg))


def test_compute_spec_bare_string() -> None:
    assert cli._compute_spec("modal", None) == {"type": "modal"}


def test_compute_spec_full_block_preserved() -> None:
    block = {"type": "modal", "region": "us"}
    assert cli._compute_spec(block, None) == {"type": "modal", "region": "us"}


def test_compute_spec_cli_matching_type_keeps_block_keys() -> None:
    block = {"type": "modal", "region": "us"}
    assert cli._compute_spec(block, "modal") == {"type": "modal", "region": "us"}


def test_compute_spec_cli_overrides_to_bare_when_type_differs() -> None:
    block = {"type": "modal", "region": "us"}
    assert cli._compute_spec(block, "docker") == {"type": "docker"}


def test_compute_spec_rejects_non_string_non_mapping() -> None:
    with pytest.raises(SystemExit):
        cli._compute_spec(["modal"], None)


def test_task_filter_normalizes_string_and_list() -> None:
    assert cli._task_filter("a, b ,") == ["a", "b"]
    assert cli._task_filter(["a", " b "]) == ["a", "b"]
    assert cli._task_filter(None) is None
    assert cli._task_filter([]) is None


def test_load_dotenv_seeds_missing_without_overriding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "# comment\nNEW_KEY=from-dotenv\nEXISTING_KEY=should-not-win\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NEW_KEY", raising=False)
    monkeypatch.setenv("EXISTING_KEY", "real-env")

    cli._load_dotenv()

    assert os.environ["NEW_KEY"] == "from-dotenv"
    assert os.environ["EXISTING_KEY"] == "real-env"  # setdefault keeps real env


def test_download_dispatches_to_download_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_download(dataset: object, dest: Path) -> Path:
        seen["dataset"] = dataset
        seen["dest"] = dest
        return dest / "x"

    monkeypatch.setattr(cli, "download_dataset", fake_download)

    rc = cli._download(argparse.Namespace(dataset="fate-m", output=str(tmp_path)))

    assert rc == 0
    assert seen["dataset"] is cli.DATASET.FATE_M
    assert seen["dest"] == tmp_path
