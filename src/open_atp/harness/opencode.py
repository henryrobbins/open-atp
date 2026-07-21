"""OpenCode CLI harness."""

from __future__ import annotations

import json
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from open_atp.auth import AuthKind, AuthStatus
from open_atp.harness._paths import _SCRIPTS
from open_atp.harness.base import (
    Harness,
    HarnessRunResult,
    MissingCredentials,
    _provider_env_var,
)

#: The authentication strategies :class:`OpenCodeHarness` supports.
_AUTH_MODES = ("api_key", "login")

#: Where opencode stores per-provider logins, under ``$XDG_DATA_HOME``'s default.
_AUTH_STORE = (".local", "share", "opencode", "auth.json")

#: opencode's data dir under the sandbox ``$HOME``; the launch script points
#: ``XDG_DATA_HOME`` here so opencode reads ``<dir>/opencode/auth.json``.
_OPENCODE_DATA_MOUNT = ".opencode-data"


class OpenCodeHarness(Harness):
    """OpenCode CLI driving any supported OpenCode model provider.

    Parameters
    ----------
    provider : str, default "deepseek"
        opencode provider name (e.g. ``"deepseek"``). Any OpenCode provider is
        accepted.
    model : str, default "deepseek-v4-pro"
        Model id the agent runs. Must be supported by the chosen provider.
    effort : str, default "high"
        Reasoning-effort level.
    auth : str, default "api_key"
        Authentication strategy, ``"api_key"`` or ``"login"``. Any other value raises
        :exc:`ValueError`. See :ref:`opencode-authentication` for details.
    api_key : str, optional
        For ``auth="api_key"``, the provider's API key. ``None`` (default) reads the
        host environment, and resolution fails if the key is set in neither. Ignored
        when ``auth="login"``.

    Examples
    --------

    By default, the harness authenticates with the provider's API key and reads
    the value from the host environment.

    >>> from open_atp.harness import OpenCodeHarness
    >>> harness = OpenCodeHarness(provider="openai", model="gpt-5.5")
    >>> harness.name
    'opencode'
    >>> harness.provider
    'openai'
    """

    name = "opencode"

    skills_dest = ".agents/skills"

    #: Holds the staged minimal opencode data dir (just the selected provider's
    #: ``auth.json`` entry) so it outlives :meth:`_home_dirs` until the backend mounts
    #: it; cleaned up on collection. Only used by the ``"login"`` auth strategy.
    _opencode_data: tempfile.TemporaryDirectory[str] | None = None

    def __init__(
        self,
        *,
        provider: str = "deepseek",
        model: str = "deepseek-v4-pro",
        effort: str = "high",
        auth: str = "api_key",
        api_key: str | None = None,
    ) -> None:
        super().__init__(model=model, effort=effort)
        if auth not in _AUTH_MODES:
            raise ValueError(f"unknown auth {auth!r}; choose from {list(_AUTH_MODES)}")
        self.provider = provider
        self.auth = auth
        self._api_key = api_key
        # Guards the lazy _opencode_data init: a benchmark sweep shares one harness
        # instance across tasks run concurrently, so check-then-create must be atomic.
        self._opencode_data_lock = threading.Lock()

    def stage_wd(self, wd: Path) -> None:
        super().stage_wd(wd)
        # opencode.json configures the model provider + MCP server.
        (wd / "opencode.json").write_text(json.dumps(self._opencode_config(), indent=2))

    def _opencode_config(self) -> dict[str, Any]:
        options: dict[str, Any]
        if self.provider == "anthropic":
            options = {
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": self.effort},
            }
        else:
            options = {"reasoningEffort": self.effort}
        return {
            "$schema": "https://opencode.ai/config.json",
            "provider": {self.provider: {"models": {self.model: {"options": options}}}},
            "mcp": {
                "lean-lsp": {
                    "type": "local",
                    "command": ["lean-lsp-mcp"],
                    "enabled": True,
                    # The first lean_diagnostic_messages call starts `lake serve` and
                    # loads the file's full import closure (Mathlib) into the LSP, which
                    # routinely exceeds opencode's 60s default MCP request timeout on a
                    # cold, few-CPU sandbox -- surfacing as `MCP error -32001: Request
                    # timed out`. Raise it so the slow first diagnostic can return.
                    "timeout": 180_000,
                }
            },
        }

    def _required_env(self) -> dict[str, str]:
        # "login" authenticates from the mounted opencode credential store (see
        # _home_dirs), so no API key is forwarded.
        if self.auth == "login":
            return {}
        return self._key_env(_provider_env_var(self.provider), self._api_key)

    def auth_status(self) -> AuthStatus:
        if self.auth != "login":
            return self._env_auth_status(
                _provider_env_var(self.provider), self._api_key
            )
        entry = self._stored_login() or {}
        # opencode stamps `expires` in epoch milliseconds.
        expires = entry.get("expires")
        return AuthStatus(
            kind=AuthKind.OAUTH,
            source=str(self._auth_store()),
            present=bool(entry),
            expires_at=(
                datetime.fromtimestamp(expires / 1000, UTC)
                if isinstance(expires, int | float)
                else None
            ),
            refreshable=bool(entry.get("refresh")),
        )

    def _auth_store(self) -> Path:
        """opencode's credential store, holding one entry per logged-in provider."""
        return Path.home().joinpath(*_AUTH_STORE)

    def _stored_login(self) -> dict[str, Any] | None:
        """This provider's entry in opencode's credential store, if it has one."""
        try:
            entry = json.loads(self._auth_store().read_text()).get(self.provider)
        except (OSError, json.JSONDecodeError):
            return None
        return entry if isinstance(entry, dict) else None

    def _home_dirs(self) -> list[tuple[Path, str]]:
        # Only "login" needs a mount; "api_key" forwards an env var instead.
        if self.auth != "login":
            return []
        # Stage a minimal data dir holding only this provider's entry -- never the
        # whole auth.json, which also carries other providers' keys -- and mount it;
        # the launch script points XDG_DATA_HOME at the mount.
        entry = self._stored_login()
        if not entry:
            raise MissingCredentials(
                f"opencode harness with auth='login' requires a {self.provider!r} "
                f"login in {self._auth_store()}; run `opencode auth login` and choose "
                f"{self.provider}"
            )
        # Lock the check-then-create: without it two concurrent runs on a shared
        # harness both see None, the second's TemporaryDirectory overwrites the first,
        # and the orphaned finalizer deletes its dir out from under the staging run.
        with self._opencode_data_lock:
            if self._opencode_data is None:
                self._opencode_data = tempfile.TemporaryDirectory(prefix="opencode-")
                auth_dir = Path(self._opencode_data.name) / "opencode"
                auth_dir.mkdir()
                auth = auth_dir / "auth.json"
                auth.write_text(json.dumps({self.provider: entry}))
                auth.chmod(0o600)
        return [(Path(self._opencode_data.name), _OPENCODE_DATA_MOUNT)]

    def _agent_command(self) -> str:
        template = (_SCRIPTS / "opencode_agent.sh").read_text()
        return template.replace("<<PROVIDER>>", self.provider).replace(
            "<<MODEL>>", self.model
        )

    def _parse_lines(self, lines: list[str]) -> HarnessRunResult:
        """Parse ``opencode run --format json`` output."""

        def _as_int(x: Any) -> int:
            return x if isinstance(x, int) else 0

        result = HarnessRunResult()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "step_finish":
                continue
            part = event.get("part") or {}
            tokens = part.get("tokens") or {}
            cache = tokens.get("cache") or {}
            result.input_tokens += (
                _as_int(tokens.get("input"))
                + _as_int(cache.get("write"))
                + _as_int(cache.get("read"))
            )
            result.output_tokens += _as_int(tokens.get("output"))
            c = part.get("cost")
            if isinstance(c, (int, float)):
                result.cost_usd = (result.cost_usd or 0.0) + float(c)
            r = part.get("reason")
            if isinstance(r, str):
                result.stop_reason = r
        return result
