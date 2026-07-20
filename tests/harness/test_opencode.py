"""Unit tests for :class:`~open_atp.harness.opencode.OpenCodeHarness`.

No compute backend or agent CLI is launched here -- these exercise the two auth
strategies (``api_key`` env forwarding vs ``login`` credential-store mount) at the
harness level. Live runs are in
``tests/harness/test_capabilities.py`` and ``tests/test_e2e_provers.py``.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from open_atp.harness import OpenCodeHarness, base


def _write_auth(home: Path, entries: dict[str, object]) -> Path:
    """Write ``entries`` as the host opencode ``auth.json`` under ``home``."""
    store = home / ".local" / "share" / "opencode" / "auth.json"
    store.parent.mkdir(parents=True)
    store.write_text(json.dumps(entries))
    return store


def test_provider_stored() -> None:
    harness = OpenCodeHarness(provider="fireworks", model="anything")
    assert harness.provider == "fireworks"


def test_invalid_auth_rejected() -> None:
    with pytest.raises(ValueError, match="unknown auth"):
        OpenCodeHarness(provider="deepseek", auth="oauth")


def test_api_key_forwards_provider_env_var() -> None:
    # Explicit key wins over the host env and is forwarded under the canonical name.
    harness = OpenCodeHarness(
        provider="deepseek", model="deepseek-v4-pro", api_key="sk-fake"
    )
    auth = harness.agent_auth()
    assert auth.env == {"DEEPSEEK_API_KEY": "sk-fake"}
    assert auth.mounts == []


def test_api_key_reads_host_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
    harness = OpenCodeHarness(
        provider="deepseek", model="deepseek-v4-pro", auth="api_key"
    )
    assert harness.agent_auth().env == {"DEEPSEEK_API_KEY": "sk-deepseek"}


def test_api_key_unpinned_provider_resolved_via_models_dev(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A provider not pinned in _PROVIDER_ENV takes the first env from models.dev
    # (whose id-derived convention would wrongly guess MOONSHOTAI_API_KEY).
    monkeypatch.setattr(
        base, "_models_dev_env", lambda: {"moonshotai": ["MOONSHOT_API_KEY"]}
    )
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-moon")
    harness = OpenCodeHarness(model="m", provider="moonshotai", auth="api_key")
    assert harness.agent_auth().env == {"MOONSHOT_API_KEY": "sk-moon"}


def test_api_key_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    harness = OpenCodeHarness(
        provider="deepseek", model="deepseek-v4-pro", auth="api_key"
    )
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        harness.agent_auth()


def test_login_mounts_only_selected_provider_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_auth(
        tmp_path,
        {
            "anthropic": {"type": "oauth", "access": "tok", "refresh": "r"},
            "deepseek": {"type": "api", "key": "sk-secret"},
        },
    )
    harness = OpenCodeHarness(
        model="claude-opus-4-8", provider="anthropic", auth="login"
    )
    auth = harness.agent_auth()

    # No API key forwarded -- opencode reads the mounted credential store.
    assert auth.env == {}
    (src, dest) = auth.mounts[0]
    assert dest == ".opencode-data"
    staged = json.loads((src / "opencode" / "auth.json").read_text())
    # Only the anthropic entry is staged -- never the deepseek key.
    assert staged == {"anthropic": {"type": "oauth", "access": "tok", "refresh": "r"}}


def test_login_missing_entry_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_auth(tmp_path, {"deepseek": {"type": "api", "key": "sk"}})
    harness = OpenCodeHarness(
        model="claude-opus-4-8", provider="anthropic", auth="login"
    )
    with pytest.raises(RuntimeError, match="requires a 'anthropic' login"):
        harness.agent_auth()


def test_login_home_dirs_is_concurrency_safe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A benchmark sweep shares one harness across parallel tasks; every concurrent
    # _home_dirs() must return the same staged dir whose auth.json survives -- without
    # the lock the loser's TemporaryDirectory finalizer deletes it.
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_auth(tmp_path, {"anthropic": {"type": "oauth", "access": "tok"}})
    harness = OpenCodeHarness(
        model="claude-opus-4-8", provider="anthropic", auth="login"
    )

    results: list[Path] = []
    barrier = threading.Barrier(8)

    def stage() -> None:
        barrier.wait()
        results.append(harness._home_dirs()[0][0])

    threads = [threading.Thread(target=stage) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len({str(p) for p in results}) == 1
    assert (results[0] / "opencode" / "auth.json").is_file()
