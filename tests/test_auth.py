"""Credential-status tests (no Docker, no creds, no network).

Two layers:

* :class:`~open_atp.auth.AuthStatus` classification -- how presence, an expiry, and
  the :data:`~open_atp.auth.EXPIRY_WARNING` threshold map onto an
  :class:`~open_atp.auth.AuthState`.
* every standard prover's ``auth_status`` reading a faked host: the env-var provers
  from a patched environment, and the file-backed OAuth ones (codex, grok, kimi)
  from credential files written into a fake ``$HOME``. Absent credentials must
  report ``MISSING`` rather than raise -- the whole point of the seam.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from open_atp.auth import EXPIRY_WARNING, AuthKind, AuthState, AuthStatus
from open_atp.backends.docker import DockerBackend
from open_atp.config import standard_prover, standard_provers

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)

#: Every standard prover's credential source, so a rename can't silently orphan the
#: env var the CLI tells a user to set.
SOURCES = {
    "claude": "CLAUDE_CODE_OAUTH_TOKEN",
    "codex": ".codex/auth.json",
    "deepseek": "DEEPSEEK_API_KEY",
    "grok": "opencode/auth.json",
    "leanstral": "MISTRAL_API_KEY",
    "axproverbase": "ANTHROPIC_API_KEY",
    "kimi": ".kimi-code/credentials",
    "numina": "CLAUDE_CODE_OAUTH_TOKEN",
    "aristotle": "ARISTOTLE_API_KEY",
}


def _status(name: str) -> AuthStatus:
    # Backend construction is offline; no Docker daemon is contacted.
    return standard_prover(name, backend=DockerBackend()).auth_status()


def _jwt(exp: datetime) -> str:
    """A signature-less JWT carrying an ``exp`` claim, as codex's auth.json holds."""
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(exp.timestamp())}).encode()
    ).decode()
    return f"header.{payload.rstrip('=')}.signature"


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """An empty fake host: no credential env vars, and a bare ``$HOME``."""
    for source in set(SOURCES.values()):
        monkeypatch.delenv(source, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("KIMI_CODE_HOME", raising=False)
    return tmp_path


# --- AuthStatus classification ---------------------------------------------


def test_absent_credential_is_missing() -> None:
    status = AuthStatus(AuthKind.API_KEY, "SOME_API_KEY", present=False)

    assert status.state(NOW) is AuthState.MISSING
    assert status.time_remaining(NOW) is None


def test_present_credential_without_expiry_stays_ok() -> None:
    status = AuthStatus(AuthKind.OAUTH, "SOME_TOKEN", present=True)

    assert status.state(NOW) is AuthState.OK
    assert status.time_remaining(NOW) is None


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (timedelta(days=9), AuthState.OK),
        (EXPIRY_WARNING, AuthState.OK),  # the threshold itself is not yet a warning
        (EXPIRY_WARNING - timedelta(seconds=1), AuthState.EXPIRING),
        (timedelta(minutes=1), AuthState.EXPIRING),
        (timedelta(0), AuthState.EXPIRED),
        (timedelta(minutes=-1), AuthState.EXPIRED),
    ],
)
def test_expiry_window_classifies_state(offset: timedelta, expected: AuthState) -> None:
    status = AuthStatus(
        AuthKind.OAUTH, "SOME_TOKEN", present=True, expires_at=NOW + offset
    )

    assert status.state(NOW) is expected
    assert status.time_remaining(NOW) == offset


def test_expiry_is_ignored_when_the_credential_is_absent() -> None:
    """An expiry read off a stale file cannot make a missing credential look valid."""
    status = AuthStatus(
        AuthKind.OAUTH, "SOME_TOKEN", present=False, expires_at=NOW + timedelta(days=1)
    )

    assert status.state(NOW) is AuthState.MISSING


# --- per-prover host reads --------------------------------------------------


@pytest.mark.parametrize("name", standard_provers())
def test_unauthenticated_host_reports_missing(name: str, host: Path) -> None:
    status = _status(name)

    assert status.state() is AuthState.MISSING
    assert not status.present
    assert SOURCES[name] in status.source


@pytest.mark.parametrize(
    ("name", "env"),
    [
        ("claude", "CLAUDE_CODE_OAUTH_TOKEN"),
        ("deepseek", "DEEPSEEK_API_KEY"),
        ("leanstral", "MISTRAL_API_KEY"),
        ("axproverbase", "ANTHROPIC_API_KEY"),
        ("aristotle", "ARISTOTLE_API_KEY"),
    ],
)
def test_env_credential_is_present_and_never_expires(
    name: str, env: str, host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(env, "secret")

    status = _status(name)

    assert status.state() is AuthState.OK
    assert status.expires_at is None
    assert not status.refreshable


def test_codex_reads_expiry_from_its_access_token(host: Path) -> None:
    expiry = datetime.now(UTC) + timedelta(days=9)
    codex = host / ".codex"
    codex.mkdir()
    (codex / "auth.json").write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": _jwt(expiry),
                    "refresh_token": "refresh-me",
                }
            }
        )
    )

    status = _status("codex")

    assert status.kind is AuthKind.OAUTH
    assert status.state() is AuthState.OK
    assert status.expires_at == expiry.replace(microsecond=0)
    assert status.refreshable


def test_codex_api_key_mode_has_no_expiry(host: Path) -> None:
    """``codex login --api-key`` writes a bare key, which authenticates indefinitely."""
    codex = host / ".codex"
    codex.mkdir()
    (codex / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "sk-fake"}))

    status = _status("codex")

    assert status.state() is AuthState.OK
    assert status.expires_at is None
    assert not status.refreshable


def test_grok_reads_its_opencode_login_entry(host: Path) -> None:
    expiry = datetime.now(UTC) + timedelta(minutes=5)
    store = host / ".local" / "share" / "opencode"
    store.mkdir(parents=True)
    (store / "auth.json").write_text(
        json.dumps(
            {
                "xai": {
                    "type": "oauth",
                    "access": "a",
                    "refresh": "r",
                    "expires": int(expiry.timestamp() * 1000),
                }
            }
        )
    )

    status = _status("grok")

    assert status.kind is AuthKind.OAUTH
    assert status.state() is AuthState.EXPIRING
    assert status.refreshable


def test_grok_ignores_another_providers_login(host: Path) -> None:
    store = host / ".local" / "share" / "opencode"
    store.mkdir(parents=True)
    (store / "auth.json").write_text(json.dumps({"anthropic": {"type": "api"}}))

    assert _status("grok").state() is AuthState.MISSING


def test_kimi_reads_its_credential_file(host: Path) -> None:
    expiry = datetime.now(UTC) - timedelta(hours=2)
    creds = host / ".kimi-code" / "credentials"
    creds.mkdir(parents=True)
    (creds / "kimi-code.json").write_text(
        json.dumps(
            {
                "access_token": "a",
                "refresh_token": "r",
                "expires_at": int(expiry.timestamp()),
            }
        )
    )

    status = _status("kimi")

    assert status.state() is AuthState.EXPIRED
    assert status.refreshable


def test_kimi_counts_a_login_under_another_provider(host: Path) -> None:
    """The harness mounts the whole credentials dir, so any login in it counts."""
    creds = host / ".kimi-code" / "credentials"
    creds.mkdir(parents=True)
    (creds / "some-other-provider.json").write_text(json.dumps({"access_token": "a"}))

    status = _status("kimi")

    assert status.state() is AuthState.OK
    assert status.expires_at is None  # no default-provider file to read one from


def test_unreadable_credential_file_reports_missing(host: Path) -> None:
    """A truncated or hand-edited store is 'no credential', never a crash."""
    codex = host / ".codex"
    codex.mkdir()
    (codex / "auth.json").write_text("{not json")

    assert _status("codex").state() is AuthState.MISSING
