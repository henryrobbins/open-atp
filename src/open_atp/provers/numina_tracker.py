"""Statement-change guard, ported from numina-lean-agent.

Ported (typed, trimmed to the proving guard's needs) from upstream
``scripts/statement_tracker.py`` + the ``LeanCodeParser`` it depends on in
``scripts/extract_sublemmas.py`` at commit
``e9e987b47f14ef818d5932a11dc026048abc35e7`` (see ``vendor/numina/VENDOR.md``).

The point of this module is to stop an agent from "proving" a goal by quietly
weakening or deleting the very theorem it was asked to prove. We snapshot the
``theorem``/``lemma`` *statements* (signatures, proof bodies stripped) of the
target files before the run, and after each round compare against that snapshot;
:class:`StatementTracker` can also restore the originals when a violation is found.

Only the regex/indentation parsing needed to isolate a theorem's statement is
ported -- the rest of upstream's ``extract_sublemmas`` (sublemma extraction,
sorry-injection helpers) is not used here.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger("open_atp")

#: Tactic-block bullet tokens; a block introduced by one of these is a proof step,
#: not a declaration, and is parsed specially.
DOT_KEYS = (".", "·")


# ---------------------------------------------------------------------------
# Lean source parser (trimmed port of extract_sublemmas.LeanCodeParser)
# ---------------------------------------------------------------------------


class LeanCodeParser:
    """Indentation-based block extractor for Lean 4 source.

    Faithful (typed) port of the upstream parser, limited to the surface
    :class:`StatementTracker` needs: comment/blank stripping plus block
    extraction for ``theorem``/``lemma`` declarations, each yielding the
    declaration's name and its statement (proof body removed).
    """

    def __init__(self, code: str) -> None:
        self.original_code = code
        self.original_lines = code.splitlines()
        self.cleaned_lines = self.strip_comments_and_blank_lines(code)
        self.cleaned_code = "\n".join(self.cleaned_lines)

    def formatting(self, code: str) -> str:
        """Normalise spacing around ``:=`` so the proof delimiter is easy to find."""
        ret = re.sub(r":=", " := ", code, flags=re.DOTALL)
        ret = re.sub(r"  :=", " :=", ret, flags=re.DOTALL)
        ret = re.sub(r":=  ", ":= ", ret, flags=re.DOTALL)
        ret = re.sub(r":= \n", ":=\n", ret, flags=re.DOTALL)
        return ret

    def strip_brackets(self, code: str) -> str:
        pattern = r"\([^()]*?\)|{[^{}]*?}|\[[^\[\]]*?\]"
        return re.sub(pattern, "", code)

    def strip_comments_and_blank_lines(self, code: str) -> list[str]:
        cleaned: list[str] = []
        tmp_code = re.sub(r"/-.*?(--/|-/)", "", code, flags=re.DOTALL)
        tmp_code = self.formatting(tmp_code)
        for line in tmp_code.splitlines():
            stripped_line = re.split(r"--", line, maxsplit=1)[0].rstrip()
            if not stripped_line.strip():
                continue
            noind_stripped = stripped_line.lstrip()
            if noind_stripped.startswith((". ", "· ")):
                indent = len(stripped_line) - len(noind_stripped)
                dot_line = " " * indent + "."
                rest_line = " " * (indent + 2) + noind_stripped[2:]
                cleaned.append(dot_line)
                if rest_line.strip():
                    cleaned.append(rest_line)
            else:
                cleaned.append(stripped_line)
        return cleaned

    def get_indent(self, line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    @staticmethod
    def extract_name_from_code(
        code: str, key: str, default: str = "this"
    ) -> str | None:
        """Pull the declared name out of a ``theorem``/``lemma`` opening line.

        ``'theorem v1 : ...'`` -> ``'v1'``; ``'theorem : ...'`` -> ``'this'``.
        """
        code = code.strip()
        if not code.startswith(f"{key} "):
            return None
        rest = code[len(key) :].strip()
        if rest.startswith((":", "(", ":=")):
            return default
        match = re.match(r"^([^ :\{\[\(]+)", rest)
        if match:
            return match.group(1)
        return None

    def extract_statement_and_proof_from_code(
        self, code: str
    ) -> tuple[str, str] | None:
        """Split a block at the top-level ``:=`` into ``(statement, proof)``."""
        if ":=" not in code:
            return None

        paren_count = 0
        bracket_count = 0
        brace_count = 0
        for i in range(len(code)):
            char = code[i]
            if char == "(":
                paren_count += 1
            elif char == "[":
                bracket_count += 1
            elif char == "{":
                brace_count += 1
            elif char == ")":
                paren_count -= 1
            elif char == "]":
                bracket_count -= 1
            elif char == "}":
                brace_count -= 1
            elif char == ":" and i + 1 < len(code) and code[i + 1] == "=":
                if paren_count == 0 and bracket_count == 0 and brace_count == 0:
                    return (code[:i].strip(), code[i + 2 :].strip())

        parts = code.split(":=", maxsplit=1)
        statement = parts[0].strip()
        proof = parts[1].strip() if len(parts) == 2 else ""
        return (statement, proof)

    def parse_block(self, lines: list[str], key: str) -> dict[str, Any]:
        """Parse a single declaration block into name/statement/proof metadata."""
        raw = "\n".join(lines)

        if key in DOT_KEYS:
            name: str | None = "."
            statement = "."
            proof = raw.lstrip()[1:].lstrip()
        else:
            name = self.extract_name_from_code(lines[0], key=key)
            statement, proof = self.extract_statement_and_proof_from_code(raw) or (
                "",
                "",
            )

        with_sorry = "sorry" in raw.split()
        proof_style = (
            "tactic"
            if proof.startswith("by")
            else ("dot_block" if key in DOT_KEYS else ("term" if proof else "unknown"))
        )

        cleaned_raw_proof = re.sub(r"^[^\n]*", "", proof, count=1)
        cleaned_raw_proof = re.sub(r"^(?:\s*\n)+", "", cleaned_raw_proof, count=1)
        inner_indent = len(cleaned_raw_proof) - len(cleaned_raw_proof.lstrip())
        if inner_indent <= 0:
            inner_indent = self.get_indent(lines[0]) + 2

        return {
            "name": name,
            "statement": statement,
            "proof": proof,
            "with_sorry": with_sorry,
            "proof_style": proof_style,
            "inner_indent": inner_indent,
        }

    def get_block_from_lid(
        self, start_line_index: int, keys: list[str] | None = None
    ) -> dict[str, Any] | None:
        """Extract the block beginning at ``cleaned_lines[start_line_index]``."""
        if keys is None:
            keys = ["theorem"]
        if start_line_index >= len(self.cleaned_lines):
            return None

        start_line = self.cleaned_lines[start_line_index]
        key = start_line.split()[0]
        if key not in keys:
            return None

        start_indent = self.get_indent(start_line)
        block_lines = [start_line]
        i = start_line_index + 1
        while i < len(self.cleaned_lines):
            line = self.cleaned_lines[i]
            line_indent = self.get_indent(line)
            if line_indent > start_indent:
                block_lines.append(line)
                i += 1
            elif not line.strip():
                i += 1
            elif line_indent == start_indent:
                if key in DOT_KEYS:
                    break
                # Statement spread over multiple unindented lines: keep going until
                # we have seen the top-level ":=".
                if ":=" not in self.strip_brackets("\n".join(block_lines)):
                    block_lines.append(line)
                    i += 1
                else:
                    break
            else:
                break

        info = self.parse_block(block_lines, key=key)
        return {
            "start": start_line_index,
            "end": i - 1,
            "lines": block_lines,
            "indent": start_indent,
            "key": key,
            "info": info,
        }

    def extract_all_blocks(
        self, keys: list[str] | None = None, allow_overlap: bool = True
    ) -> list[dict[str, Any]]:
        """Extract every block whose opening key is in ``keys``."""
        if keys is None:
            keys = ["theorem"]
        blocks: list[dict[str, Any]] = []
        i = 0
        while i < len(self.cleaned_lines):
            block = self.get_block_from_lid(i, keys=keys)
            if block:
                blocks.append(block)
                if not allow_overlap:
                    i = block["end"]
            i += 1
        return blocks


# ---------------------------------------------------------------------------
# Statement snapshotting + tracking
# ---------------------------------------------------------------------------


def extract_statements_from_file(file_path: Path) -> dict[str, str]:
    """Map each ``theorem``/``lemma`` name in ``file_path`` to its statement."""
    try:
        if not file_path.exists():
            return {}
        code = file_path.read_text(encoding="utf-8")
        parser = LeanCodeParser(code)
        blocks = parser.extract_all_blocks(
            keys=["theorem", "lemma"], allow_overlap=False
        )
        statements: dict[str, str] = {}
        for block in blocks:
            name = block["info"]["name"]
            statement = block["info"]["statement"]
            if name:
                statements[name] = statement
        return statements
    except Exception:  # noqa: BLE001 - parsing is best-effort; never crash a run
        log.warning(
            "statement extraction failed", extra={"file": str(file_path)}, exc_info=True
        )
        return {}


def normalize_statement(statement: str) -> str:
    """Collapse whitespace so cosmetic reformatting is not flagged as a change."""
    return " ".join(statement.split())


ChangeType = Literal["modified", "added", "removed"]


@dataclass
class StatementChange:
    """One detected difference between a file's initial and current statements.

    Parameters
    ----------
    file_path : pathlib.Path
        The Lean file the change was found in.
    name : str
        Declaration name of the affected theorem or lemma.
    original : str
        The statement as captured in the initial snapshot; empty when added.
    current : str
        The statement as it now stands; empty when removed.
    change_type : {"modified", "added", "removed"}
        How the statement differs from the snapshot.
    """

    file_path: Path
    name: str
    original: str
    current: str
    change_type: ChangeType

    def __str__(self) -> str:
        return f"[{self.change_type}] {self.file_path}:{self.name}"


class StatementTracker:
    """Track theorem/lemma statement changes across agent rounds.

    Snapshots the tracked files on construction; :meth:`check` reports diffs
    against that snapshot, :meth:`check_initial_statements` reports only the
    disallowed (modified/removed) ones, and :meth:`restore_initial_statements`
    puts the originals back.
    """

    def __init__(self, files: list[Path]) -> None:
        self.files = [Path(f).resolve() for f in files]
        self.initial_snapshots: dict[Path, dict[str, str]] = {}
        self.initial_file_contents: dict[Path, str] = {}
        self._capture_initial()

    def _capture_initial(self) -> None:
        for f in self.files:
            if f.exists():
                self.initial_file_contents[f] = f.read_text(encoding="utf-8")
            self.initial_snapshots[f] = extract_statements_from_file(f)

    def check(self) -> list[StatementChange]:
        """Diff every tracked file's current statements against the snapshot."""
        changes: list[StatementChange] = []
        for f in self.files:
            initial = self.initial_snapshots.get(f, {})
            current = extract_statements_from_file(f)
            for name in set(initial) | set(current):
                orig = initial.get(name, "")
                curr = current.get(name, "")
                if normalize_statement(orig) != normalize_statement(curr):
                    if not orig:
                        change_type: ChangeType = "added"
                    elif not curr:
                        change_type = "removed"
                    else:
                        change_type = "modified"
                    changes.append(
                        StatementChange(
                            file_path=f,
                            name=name,
                            original=orig,
                            current=curr,
                            change_type=change_type,
                        )
                    )
        return changes

    def check_initial_statements(self) -> tuple[bool, list[StatementChange]]:
        """Return ``(is_valid, disallowed_changes)``.

        ``is_valid`` is True iff no initial statement was modified or removed
        (newly *added* statements are allowed and excluded from the list).
        """
        relevant = [c for c in self.check() if c.change_type in ("modified", "removed")]
        return (len(relevant) == 0, relevant)

    def get_initial_statements(self) -> dict[Path, dict[str, str]]:
        return {f: dict(s) for f, s in self.initial_snapshots.items()}

    def restore_initial_statements(
        self, changes: list[StatementChange] | None = None
    ) -> None:
        """Restore tracked files to their original statements.

        Re-creates deleted files from the captured content and rewrites modified
        or removed declarations back to their snapshotted form (a removed one is
        re-appended with a ``sorry`` proof).
        """
        if changes is None:
            _, changes = self.check_initial_statements()
        if not changes:
            log.debug("no statement changes to restore")
            return

        changes_by_file: dict[Path, list[StatementChange]] = {}
        for change in changes:
            changes_by_file.setdefault(change.file_path, []).append(change)

        # Restore wholesale any file that was deleted.
        restored_files: set[Path] = set()
        for f in self.files:
            initial_content = self.initial_file_contents.get(f)
            if not initial_content:
                continue
            if not f.exists():
                log.warning(
                    "agent deleted tracked file; restoring", extra={"file": str(f)}
                )
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(initial_content, encoding="utf-8")
                restored_files.add(f)

        for f, file_changes in changes_by_file.items():
            if f in restored_files or not f.exists():
                continue
            initial_statements = self.initial_snapshots.get(f, {})
            initial_content = self.initial_file_contents.get(f, "")
            if not initial_statements or not initial_content:
                continue

            current_content = f.read_text(encoding="utf-8")
            new_content = current_content

            parser = LeanCodeParser(current_content)
            current_blocks = parser.extract_all_blocks(
                keys=["theorem", "lemma"], allow_overlap=False
            )
            current_block_map: dict[str, str] = {}
            for block in current_blocks:
                name = block.get("info", {}).get("name")
                if name:
                    current_block_map[name] = "\n".join(block.get("lines", []))

            for change in file_changes:
                if change.change_type == "removed":
                    if change.name in initial_statements:
                        original_stmt = initial_statements[change.name]
                        new_content = (
                            new_content + "\n\n" + original_stmt + " := by sorry"
                        )
                        log.info(
                            "restored removed statement",
                            extra={"statement": change.name, "file": str(f)},
                        )
                elif change.change_type == "modified":
                    if (
                        change.name in initial_statements
                        and change.name in current_block_map
                    ):
                        current_block_text = current_block_map[change.name]
                        orig_parser = LeanCodeParser(initial_content)
                        orig_blocks = orig_parser.extract_all_blocks(
                            keys=["theorem", "lemma"], allow_overlap=False
                        )
                        orig_block_text: str | None = None
                        for block in orig_blocks:
                            if block.get("info", {}).get("name") == change.name:
                                orig_block_text = "\n".join(block.get("lines", []))
                                break
                        if orig_block_text:
                            new_content = new_content.replace(
                                current_block_text, orig_block_text
                            )
                            log.info(
                                "restored modified statement",
                                extra={"statement": change.name, "file": str(f)},
                            )

            if new_content != current_content:
                f.write_text(new_content, encoding="utf-8")
                log.debug(
                    "rewrote file with restored statements", extra={"file": str(f)}
                )
