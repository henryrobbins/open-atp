from importlib.metadata import version as _pkg_version

from docutils import nodes
from sphinx.application import Sphinx
from sphinx.environment import BuildEnvironment

project = "open-afps"
author = "Henry Robbins"
copyright = "2026, Henry Robbins"
release = _pkg_version("open-afps")

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.extlinks",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
    "sphinx_design",
    "numpydoc",
]

extlinks = {
    "claude": ("https://code.claude.com/docs/en%s", "Claude Code Docs%.0s"),
    "codex": ("https://developers.openai.com/codex%s", "Codex Docs%.0s"),
    "opencode": ("https://opencode.ai/docs%s", "OpenCode Docs%.0s"),
    "github": (
        "https://github.com/henryrobbins/open-afps%s",
        "GitHub%.0s",
    ),
}

myst_enable_extensions = [
    "colon_fence",
    "deflist",
    "fieldlist",
    "dollarmath",
    "amsmath",
    "substitution",
]

myst_substitutions = {
    "Claude Code Docs": "[Claude Code Docs](https://code.claude.com/docs/en)",
    "Codex Docs": "[Codex Docs](https://developers.openai.com/codex)",
    "OpenCode Docs": "[OpenCode Docs](https://opencode.ai/docs)",
    "GitHub": "[GitHub](https://github.com/henryrobbins/open-afps)",
}

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

templates_path = ["_templates"]
# PHASE*.md are internal design notes, not part of the rendered site.
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store", "PHASE*.md"]

html_theme = "furo"
html_title = "open-afps"
html_static_path = ["_static"]
html_css_files = ["custom.css"]

autodoc_default_options = {"members": True, "undoc-members": True}
autodoc_typehints = "none"
numpydoc_class_members_toctree = False
numpydoc_show_class_members = False
numpydoc_xref_param_type = True
numpydoc_xref_ignore = {"of", "or", "optional", "default"}
numpydoc_xref_aliases = {
    # Platform
    "Platform": "open_afps.api.Platform",
    "SolveResult": "open_afps.api.SolveResult",
    "build_prover": "open_afps.api.build_prover",
    # Core
    "LeanProject": "open_afps.core.task.LeanProject",
    "ProofTask": "open_afps.core.task.ProofTask",
    "ToolchainMismatch": "open_afps.core.task.ToolchainMismatch",
    "VerificationReport": "open_afps.core.result.VerificationReport",
    "GenerationOutput": "open_afps.core.result.GenerationOutput",
    "ProofResult": "open_afps.core.result.ProofResult",
    "Verifier": "open_afps.core.verifier.Verifier",
    "AutomatedProver": "open_afps.core.prover.AutomatedProver",
    "AutomatedProverConfig": "open_afps.core.prover.AutomatedProverConfig",
    # Backends
    "ComputeBackend": "open_afps.backends.base.ComputeBackend",
    "BackendConfig": "open_afps.backends.base.BackendConfig",
    "CommandHandle": "open_afps.backends.base.CommandHandle",
    "CommandResult": "open_afps.backends.base.CommandResult",
    "DockerBackend": "open_afps.backends.docker.DockerBackend",
    "DockerConfig": "open_afps.backends.docker.DockerConfig",
    "ModalBackend": "open_afps.backends.modal.ModalBackend",
    "ModalConfig": "open_afps.backends.modal.ModalConfig",
    # Provers
    "AgentProver": "open_afps.provers.agent_prover.AgentProver",
    "AgentProverConfig": "open_afps.provers.agent_prover.AgentProverConfig",
    "AristotleProver": "open_afps.provers.aristotle.AristotleProver",
    "AristotleProverConfig": "open_afps.provers.aristotle.AristotleProverConfig",
    "NuminaProver": "open_afps.provers.numina.NuminaProver",
    "NuminaProverConfig": "open_afps.provers.numina.NuminaProverConfig",
    # Harnesses
    "Harness": "open_afps.harness.base.Harness",
    "HarnessRunResult": "open_afps.harness.base.HarnessRunResult",
    "AuthSpec": "open_afps.harness.base.AuthSpec",
    "ClaudeCodeHarness": "open_afps.harness.claude_code.ClaudeCodeHarness",
    "CodexHarness": "open_afps.harness.codex.CodexHarness",
    "OpenCodeHarness": "open_afps.harness.opencode.OpenCodeHarness",
    "VibeHarness": "open_afps.harness.vibe.VibeHarness",
    "AxProverHarness": "open_afps.harness.axprover.AxProverHarness",
    "AssetBundle": "open_afps.harness.bundles.AssetBundle",
    "COST_PER_MTOK": "open_afps.harness.cost.COST_PER_MTOK",
    "compute_cost_usd": "open_afps.harness.cost.compute_cost_usd",
    # Modal SDK types referenced in ModalBackend docstrings. Resolved to the Modal
    # docs by the ``missing-reference`` handler in ``setup`` below.
    "Sandbox": "modal.Sandbox",
    "Image": "modal.Image",
    "App": "modal.App",
    "Secret": "modal.Secret",
}

# Modal publishes no Sphinx ``objects.inv``, so intersphinx cannot resolve its
# types. Map the Modal symbols referenced in our docstrings to their pages in the
# Modal Python SDK reference instead. Keys are the (numpydoc-aliased) xref targets;
# values are paths under ``_MODAL_SDK_BASE``.
_MODAL_SDK_BASE = "https://modal.com/docs/sdk/py/latest/"
_MODAL_OBJECTS = {
    "modal.Sandbox": "modal.Sandbox",
    "modal.Image": "modal.Image",
    "modal.App": "modal.App",
    "modal.Secret": "modal.Secret",
}


def _resolve_modal_xref(
    app: Sphinx,
    env: BuildEnvironment,
    node: nodes.Element,
    contnode: nodes.Element,
) -> nodes.reference | None:
    """Resolve unresolved Modal xrefs to the Modal Python SDK reference."""
    path = _MODAL_OBJECTS.get(node.get("reftarget", ""))
    if path is None:
        return None
    ref = nodes.reference("", "", internal=False, refuri=_MODAL_SDK_BASE + path)
    ref.append(contnode)
    return ref


def setup(app: Sphinx) -> None:
    app.connect("missing-reference", _resolve_modal_xref)
