import inspect
import sys
from importlib.metadata import version as _pkg_version
from pathlib import Path

from docutils import nodes
from sphinx.application import Sphinx
from sphinx.environment import BuildEnvironment

# Local extensions (docs/_ext): provers_table generates the prover comparison
# table from docs/provers.yaml.
sys.path.insert(0, str(Path(__file__).parent / "_ext"))

project = "OpenATP"
author = "Henry Robbins"
copyright = "2026, Henry Robbins"
release = _pkg_version("open-atp")

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.doctest",
    "sphinx.ext.extlinks",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
    "sphinx_design",
    "sphinxarg.ext",
    "numpydoc",
    "sphinxcontrib.bibtex",
    "provers_table",
]

bibtex_bibfiles = ["ref.bib"]
bibtex_default_style = "plain"

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
    "attrs_inline",
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

# The ``{testcode}`` example blocks construct real provers so imports, class
# names, and keyword arguments are typo-checked by ``sphinx-build -b doctest``.
# They must not run an actual proof, though — that needs Docker, agent
# credentials, and real cost — so stub ``prove`` for the doctest build. Every
# prover inherits this one method from ``AutomatedProver``.
doctest_global_setup = """
from unittest import mock
from open_atp.provers.base import AutomatedProver
AutomatedProver.prove = mock.MagicMock(return_value=mock.MagicMock(success=True))
"""

templates_path = ["_templates"]
# PHASE*.md are internal design notes, not part of the rendered site.
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store", "PHASE*.md"]

html_theme = "furo"
html_title = "OpenA⊢P"
html_static_path = ["_static"]
html_css_files = ["custom.css"]

# Lean-inspired Furo theme. ``custom.css`` overrides the full set of Furo
# variables; the brand colors are surfaced here too so they stay in sync even
# before the stylesheet loads. See ``design_handoff_furo_lean_theme/``.
html_theme_options = {
    # The logos are full wordmarks, so hide the redundant text title and let Furo
    # swap between them based on the active light/dark color scheme.
    "sidebar_hide_name": True,
    "light_logo": "logo_light.svg",
    "dark_logo": "logo_dark.svg",
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

autodoc_default_options = {"members": True}
autodoc_typehints = "signature"
# Render members in definition order rather than alphabetically: classes define
# their ``@property`` accessors right after ``__init__``, so properties lead and
# the methods that follow read in a deliberate order. (``groupwise`` would sort
# methods *before* properties -- autodoc scores them 50 vs. 60.)
autodoc_member_order = "bysource"
numpydoc_class_members_toctree = False
numpydoc_show_class_members = False
numpydoc_xref_param_type = True
numpydoc_xref_ignore = {"of", "or", "optional", "default"}
# Maps a bare name in a numpydoc *type* field to its full path, so
# ``backend : ComputeBackend`` links. Only type fields are rewritten -- prose uses
# explicit ``:class:``/``:func:`` roles and never consults this table, so only
# names that can appear as a *type* belong here (no functions, no constants).
numpydoc_xref_aliases = {
    # Images
    "DEFAULT_IMAGE": "open_atp.images.DEFAULT_IMAGE",
    "SKELETON_DIR": "open_atp.images.SKELETON_DIR",
    # Lean input contract
    "LeanProject": "open_atp.lean.LeanProject",
    "ProofTask": "open_atp.lean.ProofTask",
    "ToolchainMismatch": "open_atp.lean.ToolchainMismatch",
    "MathlibRevMismatch": "open_atp.lean.MathlibRevMismatch",
    # Verification
    "VerificationReport": "open_atp.verify.VerificationReport",
    "Verifier": "open_atp.verify.Verifier",
    "ProofResult": "open_atp.provers.base.ProofResult",
    "AutomatedProver": "open_atp.provers.base.AutomatedProver",
    # Backends
    "ComputeBackend": "open_atp.backends.base.ComputeBackend",
    "CommandHandle": "open_atp.backends.base.CommandHandle",
    "CommandResult": "open_atp.backends.base.CommandResult",
    "DockerBackend": "open_atp.backends.docker.DockerBackend",
    "ModalBackend": "open_atp.backends.modal.ModalBackend",
    # Provers
    "AgentProver": "open_atp.provers.agent_prover.AgentProver",
    "AristotleProver": "open_atp.provers.aristotle.AristotleProver",
    "NuminaProver": "open_atp.provers.numina.NuminaProver",
    # Harnesses
    "Harness": "open_atp.harness.base.Harness",
    "HarnessRunResult": "open_atp.harness.base.HarnessRunResult",
    "AgentAuth": "open_atp.harness.base.AgentAuth",
    "ClaudeCodeHarness": "open_atp.harness.claude_code.ClaudeCodeHarness",
    "CodexHarness": "open_atp.harness.codex.CodexHarness",
    "OpenCodeHarness": "open_atp.harness.opencode.OpenCodeHarness",
    "VibeHarness": "open_atp.harness.vibe.VibeHarness",
    "AxProverBaseHarness": "open_atp.harness.axproverbase.AxProverBaseHarness",
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


def _skip_data_attributes(
    app: Sphinx,
    what: str,
    name: str,
    obj: object,
    skip: bool,
    options: object,
) -> bool | None:
    """Only enumerate *methods* and *properties* as class members.

    Constructor parameters and plain instance state are documented in the class
    docstring's ``Parameters``/``Attributes`` sections, which numpydoc renders.
    Letting autodoc also emit them as members duplicates every entry, so data
    attributes and class vars are dropped. A ``@property`` is documented in its
    own docstring, like a method, so it is kept: numpydoc rewrites an
    ``Attributes`` entry that names a property with the first sentence of that
    property's docstring, which silently discards whatever prose the class
    docstring wrote for it.
    """
    if skip:
        return skip
    if what == "class" and not (inspect.isroutine(obj) or isinstance(obj, property)):
        return True
    return None


#: Prefix sphinx-argparse uses for an argument's choice list (sphinxarg.ext renders
#: it as a flat ``Possible choices: a, b`` paragraph with no inline markup).
_CHOICES_PREFIX = "Possible choices: "


def _codeify_arg_choices(app: Sphinx, doctree: nodes.document) -> None:
    """Wrap each value in a sphinx-argparse ``Possible choices:`` line in ``literal``.

    sphinx-argparse emits the choices as a single plain-text paragraph; rebuild it so
    each choice renders as inline code on the CLI reference page.
    """
    for para in list(doctree.findall(nodes.paragraph)):
        text = para.astext()
        if not text.startswith(_CHOICES_PREFIX):
            continue
        choices = [c.strip() for c in text[len(_CHOICES_PREFIX) :].split(",")]
        children: list[nodes.Node] = [nodes.Text(_CHOICES_PREFIX)]
        for i, choice in enumerate(choices):
            if i:
                children.append(nodes.Text(", "))
            children.append(nodes.literal(text=choice))
        para.children = children


def setup(app: Sphinx) -> None:
    app.connect("missing-reference", _resolve_modal_xref)
    app.connect("autodoc-skip-member", _skip_data_attributes)
    app.connect("doctree-read", _codeify_arg_choices)
