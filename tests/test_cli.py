"""CLI tests for the benchmark/download commands (no Docker, no network).

Covers the YAML provers-config parsing (single string, list of names + config
mappings, name derivation/collision, defaults to all standard provers) and that
``download`` dispatches to :func:`~open_atp.benchmark.download_dataset` with the right
:class:`~open_atp.benchmark.DATASET`. Building a prover constructs it but does not run
it, so no backend is exercised.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from open_atp import __main__ as cli
from open_atp.config import build_backend, standard_provers


def _backend() -> object:
    return build_backend({"type": "docker"})  # constructed, never started


def test_no_config_uses_all_standard_provers(tmp_path: Path) -> None:
    provers = cli._load_provers(tmp_path, None, _backend())
    assert sorted(provers) == sorted(standard_provers())


def test_single_string_is_one_standard_prover(tmp_path: Path) -> None:
    cfg = tmp_path / "p.yaml"
    cfg.write_text("numina\n")
    assert sorted(cli._load_provers(tmp_path, str(cfg), _backend())) == ["numina"]


def test_list_of_names_and_config_mappings(tmp_path: Path) -> None:
    cfg = tmp_path / "p.yaml"
    cfg.write_text(
        "- agent:claude\n"
        "- type: agent\n"
        "  harness:\n"
        "    type: codex\n"
        "    model: gpt-5.5\n"
        "- name: my-numina\n"
        "  type: numina\n"
    )

    provers = cli._load_provers(tmp_path, str(cfg), _backend())

    assert sorted(provers) == ["agent:claude", "agent:codex", "my-numina"]
    assert provers["agent:codex"].harness.model == "gpt-5.5"  # type: ignore[attr-defined]


def test_provers_yaml_autodetected_in_directory(tmp_path: Path) -> None:
    (tmp_path / "provers.yaml").write_text("aristotle\n")
    assert sorted(cli._load_provers(tmp_path, None, _backend())) == ["aristotle"]


def test_duplicate_derived_names_are_suffixed(tmp_path: Path) -> None:
    cfg = tmp_path / "p.yaml"
    cfg.write_text("- numina\n- type: numina\n")

    provers = cli._load_provers(tmp_path, str(cfg), _backend())

    assert sorted(provers) == ["numina", "numina-1"]


def test_invalid_entry_rejected(tmp_path: Path) -> None:
    cfg = tmp_path / "p.yaml"
    cfg.write_text("- 123\n")
    with pytest.raises(SystemExit):
        cli._load_provers(tmp_path, str(cfg), _backend())


def test_download_dispatches_to_download_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_download(dataset: object, dest: Path) -> Path:
        seen["dataset"] = dataset
        seen["dest"] = dest
        return dest / "x"

    monkeypatch.setattr(cli, "download_dataset", fake_download)

    rc = cli._download(argparse.Namespace(dataset="fate-m", dest=str(tmp_path)))

    assert rc == 0
    assert seen["dataset"] is cli.DATASET.FATE_M
    assert seen["dest"] == tmp_path
