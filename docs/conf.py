from importlib.metadata import version as _pkg_version

from docutils import nodes
from sphinx.application import Sphinx
from sphinx.environment import BuildEnvironment

project = "OpenATP"
author = "Henry Robbins"
copyright = "2026, Henry Robbins"
release = _pkg_version("open-atp")

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
        "https://github.com/henryrobbins/open-atp%s",
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
    "GitHub": "[GitHub](https://github.com/henryrobbins/open-atp)",
}

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

templates_path = ["_templates"]
# PHASE*.md are internal design notes, not part of the rendered site.
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store", "PHASE*.md"]

html_theme = "furo"
html_title = "OpenATP"
html_static_path = ["_static"]
html_css_files = ["custom.css"]

# Lean-inspired Furo theme. ``custom.css`` overrides the full set of Furo
# variables; the brand colors are surfaced here too so they stay in sync even
# before the stylesheet loads. See ``design_handoff_furo_lean_theme/``.
html_theme_options = {
    "sidebar_hide_name": False,
    "light_css_variables": {
        "color-brand-primary": "#3D6AC9",
        "color-brand-content": "#3D6AC9",
    },
    "dark_css_variables": {
        "color-brand-primary": "#7ba2ea",
        "color-brand-content": "#8fb1ef",
    },
    "source_repository": "https://github.com/henryrobbins/open-atp/",
    "source_branch": "main",
    "source_directory": "docs/",
    "footer_icons": [
        {
            "name": "GitHub",
            "url": "https://github.com/henryrobbins/open-atp",
            "html": "",
            "class": "fa-brands fa-github",
        },
    ],
}

# Pygments themes the tokens inside code blocks; these pair with the Lean palette.
pygments_style = "friendly"
pygments_dark_style = "github-dark"

autodoc_default_options = {"members": True, "undoc-members": True}
autodoc_typehints = "none"
numpydoc_class_members_toctree = False
numpydoc_show_class_members = False
numpydoc_xref_param_type = True
numpydoc_xref_ignore = {"of", "or", "optional", "default"}
numpydoc_xref_aliases = {
    # Registry
    "PROVERS": "open_atp.provers.PROVERS",
    "get_prover": "open_atp.provers.get_prover",
    "stage_files": "open_atp.lean.stage_files",
    # Lean input contract
    "LeanProject": "open_atp.lean.LeanProject",
    "ProofTask": "open_atp.lean.ProofTask",
    "ToolchainMismatch": "open_atp.lean.ToolchainMismatch",
    # Verification
    "VerificationReport": "open_atp.verify.VerificationReport",
    "ProofResult": "open_atp.verify.ProofResult",
    "Verifier": "open_atp.verify.Verifier",
    "AutomatedProver": "open_atp.provers.base.AutomatedProver",
    "AutomatedProverConfig": "open_atp.provers.base.AutomatedProverConfig",
    # Backends
    "ComputeBackend": "open_atp.backends.base.ComputeBackend",
    "BackendConfig": "open_atp.backends.base.BackendConfig",
    "CommandHandle": "open_atp.backends.base.CommandHandle",
    "CommandResult": "open_atp.backends.base.CommandResult",
    "DockerBackend": "open_atp.backends.docker.DockerBackend",
    "DockerConfig": "open_atp.backends.docker.DockerConfig",
    "ModalBackend": "open_atp.backends.modal.ModalBackend",
    "ModalConfig": "open_atp.backends.modal.ModalConfig",
    # Provers
    "AgentProver": "open_atp.provers.agent_prover.AgentProver",
    "AgentProverConfig": "open_atp.provers.agent_prover.AgentProverConfig",
    "AristotleProver": "open_atp.provers.aristotle.AristotleProver",
    "AristotleProverConfig": "open_atp.provers.aristotle.AristotleProverConfig",
    "NuminaProver": "open_atp.provers.numina.NuminaProver",
    "NuminaProverConfig": "open_atp.provers.numina.NuminaProverConfig",
    # Harnesses
    "Harness": "open_atp.harness.base.Harness",
    "HarnessRunResult": "open_atp.harness.base.HarnessRunResult",
    "AuthSpec": "open_atp.harness.base.AuthSpec",
    "ClaudeCodeHarness": "open_atp.harness.claude_code.ClaudeCodeHarness",
    "CodexHarness": "open_atp.harness.codex.CodexHarness",
    "OpenCodeHarness": "open_atp.harness.opencode.OpenCodeHarness",
    "VibeHarness": "open_atp.harness.vibe.VibeHarness",
    "AxProverHarness": "open_atp.harness.axprover.AxProverHarness",
    "AssetBundle": "open_atp.harness.bundles.AssetBundle",
    "COST_PER_MTOK": "open_atp.harness.cost.COST_PER_MTOK",
    "compute_cost_usd": "open_atp.harness.cost.compute_cost_usd",
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
