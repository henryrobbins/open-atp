"""What each prover authenticates with, and for how long it stays valid.

Every prover needs a credential before it can run, but they don't take the same
shape: some read an API key from the environment, others carry an OAuth token
minted by a ``login`` command and stored on disk. This module is the vocabulary
for reporting on either -- :class:`AuthStatus` is what a prover (or its harness)
returns when asked *"can you run right now, and for how much longer?"*, and the
``auth-status`` CLI command tabulates one per standard prover.

An OAuth credential that ships a refresh token renews itself on the host whenever
its CLI runs. That renewal does **not** happen inside a sandbox: the credential is
copied in, so a token refreshed there is discarded with the container while the
host copy stays stale. An expired-but-refreshable credential therefore still needs
a host-side refresh (run the CLI, or log in again) before a run will authenticate.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

#: How much validity is left before a credential is reported as
#: :attr:`AuthState.EXPIRING` rather than :attr:`AuthState.OK`.
EXPIRY_WARNING = timedelta(minutes=15)


class AuthKind(enum.Enum):
    """The kind of credential a prover authenticates with."""

    #: A provider API key, read from an environment variable.
    API_KEY = "api key"
    #: An OAuth token minted by a ``login`` command, stored in the environment or
    #: in the CLI's own credential file.
    OAUTH = "oauth"


class AuthState(enum.Enum):
    """Whether a credential is usable, and if not, why."""

    #: Present, and either non-expiring or valid for more than
    #: :data:`EXPIRY_WARNING`.
    OK = "ok"
    #: Present and valid, but for less than :data:`EXPIRY_WARNING`.
    EXPIRING = "expiring"
    #: Present, but its validity window has passed.
    EXPIRED = "expired"
    #: Not found in the environment or on disk.
    MISSING = "missing"


@dataclass(frozen=True)
class AuthStatus:
    """The state of one prover's credential, as read from the host.

    Reported by :meth:`~open_atp.provers.base.AutomatedProver.auth_status`. Reading
    the host never validates the credential against its provider -- a present,
    unexpired token can still be revoked or wrong.

    Parameters
    ----------
    kind : AuthKind
        Whether the prover authenticates with an API key or an OAuth token.
    source : str
        Where the credential is read from: an environment variable name, or the
        path of the CLI credential file.
    present : bool
        Whether the credential was found at ``source``.
    expires_at : datetime.datetime, optional
        When the credential stops being valid. ``None`` (the default) when it does
        not expire or exposes no expiry -- an API key, or an opaque long-lived
        token.
    refreshable : bool, default False
        Whether the credential ships a refresh token, letting its CLI renew it on
        the host. Sandboxed runs cannot refresh it themselves.
    remedy : str, optional
        How to obtain the credential, as a fragment naming the command or key to
        set (``"`codex login`"``). Reported when one is missing. Defaults to no
        hint beyond :attr:`source`.
    """

    kind: AuthKind
    source: str
    present: bool
    expires_at: datetime | None = None
    refreshable: bool = False
    remedy: str = ""

    def time_remaining(self, now: datetime | None = None) -> timedelta | None:
        """How long the credential stays valid, relative to ``now``.

        Parameters
        ----------
        now : datetime.datetime, optional
            The instant to measure from; defaults to the current UTC time.

        Returns
        -------
        datetime.timedelta or None
            Time until expiry, negative once past it. ``None`` when the credential
            is absent or exposes no expiry.
        """
        if not self.present or self.expires_at is None:
            return None
        return self.expires_at - (now or datetime.now(UTC))

    def state(self, now: datetime | None = None) -> AuthState:
        """Classify the credential as of ``now``.

        Parameters
        ----------
        now : datetime.datetime, optional
            The instant to classify against; defaults to the current UTC time.

        Returns
        -------
        AuthState
            :attr:`~AuthState.MISSING` if absent, else the position of ``now`` in
            the validity window. A credential with no expiry is always
            :attr:`~AuthState.OK` when present.

        Examples
        --------
        A key read from the environment neither expires nor refreshes:

        >>> from open_atp.auth import AuthKind, AuthStatus
        >>> status = AuthStatus(AuthKind.API_KEY, "OPENAI_API_KEY", present=True)
        >>> status.state()
        <AuthState.OK: 'ok'>
        """
        if not self.present:
            return AuthState.MISSING
        remaining = self.time_remaining(now)
        if remaining is None:
            return AuthState.OK
        if remaining <= timedelta(0):
            return AuthState.EXPIRED
        return AuthState.EXPIRING if remaining < EXPIRY_WARNING else AuthState.OK
