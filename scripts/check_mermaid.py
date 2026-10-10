#!/usr/bin/env python3
"""
Mermaid Diagram Syntax Linter & Validator.

Scans Markdown documentation files for ```mermaid code blocks and validates
their syntax, catching common syntax and parsing errors such as:
- Unquoted parentheses, brackets, or braces in flowchart edge labels:
    Server -->|tracker-arm (Signed Binary & Manifest)| HttpEngine  # INVALID
    Server -->|"tracker-arm (Signed Binary & Manifest)"| HttpEngine  # VALID
- Unquoted nested delimiters in node shape definitions:
    Node[Text (Extra)]   # INVALID
    Node["Text (Extra)"] # VALID
- Unclosed edge label pipes (|...|)
- Unclosed or mismatched subgraphs and sequence blocks
- Unbalanced quotes
- Invalid diagram type declarations

Zero external runtime dependencies. Runs offline and cross-platform.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

VALID_DIAGRAM_TYPES = {
    "flowchart",
    "graph",
    "sequenceDiagram",
    "classDiagram",
    "stateDiagram",
    "stateDiagram-v2",
    "erDiagram",
    "gantt",
    "pie",
    "gitGraph",
    "mindmap",
    "quadrantChart",
    "xychart-beta",
    "journey",
    "timeline",
    "C4Context",
    "C4Container",
    "C4Component",
    "C4Dynamic",
    "C4Deployment",
    "requirementDiagram",
    "packet-beta",
    "architecture-beta",
    "kanban",
    "block-beta",
}

# Supported flowchart arrow prefixes followed by |label|
EDGE_PIPE_PATTERN = re.compile(
    r"(?:--+>|---+|-\.-+>|\==+>|<--+|<--+>|o--o|x--x|--o|--x)\s*\|([^|\n]+)\|"
)

# Unclosed pipe detector: arrow followed by a single pipe without closing pipe
UNCLOSED_PIPE_PATTERN = re.compile(
    r"(?:--+>|---+|-\.-+>|\==+>|<--+|<--+>|o--o|x--x|--o|--x)\s*\|"
)


@dataclass
class LintError:
    file_path: str
    line_number: int
    message: str
    line_content: str | None = None

    def __str__(self) -> str:
        loc = f"{self.file_path}:{self.line_number}"
        res = f"❌ {loc}: {self.message}"
        if self.line_content:
            res += f"\n     Line: {self.line_content.strip()}"
        return res


def extract_mermaid_blocks(content: str) -> list[tuple[int, list[tuple[int, str]]]]:
    """
    Extracts all ```mermaid blocks from a Markdown string.
    Returns a list of tuples: (block_start_line, list_of_(line_no, line_text)).
    Line numbers are 1-indexed.
    """
    blocks: list[tuple[int, list[tuple[int, str]]]] = []
    in_mermaid = False
    current_block: list[tuple[int, str]] = []
    block_start = 0

    for line_no, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if not in_mermaid:
            if stripped.startswith("```mermaid"):
                in_mermaid = True
                current_block = []
                block_start = line_no
        else:
            if stripped == "```":
                blocks.append((block_start, current_block))
                in_mermaid = False
            else:
                current_block.append((line_no, line))

    if in_mermaid:
        # Dangling block without closing ```
        blocks.append((block_start, current_block))

    return blocks


def validate_mermaid_block(
    file_path: str,
    block_start_line: int,
    lines: list[tuple[int, str]],
) -> list[LintError]:
    """
    Validates a single Mermaid diagram block.
    """
    errors: list[LintError] = []

    if not lines:
        errors.append(
            LintError(file_path, block_start_line, "Empty Mermaid code block")
        )
        return errors

    # 1. Identify diagram type declaration
    diag_type: str | None = None
    diag_type_line = block_start_line

    for line_no, text in lines:
        s = text.strip()
        if not s or s.startswith("%%"):
            continue
        diag_type = s.split()[0]
        diag_type_line = line_no
        break

    if not diag_type or diag_type not in VALID_DIAGRAM_TYPES:
        found_desc = f"'{diag_type}'" if diag_type else "none"
        errors.append(
            LintError(
                file_path,
                diag_type_line,
                f"Invalid or missing Mermaid diagram type declaration (found {found_desc}).",
            )
        )
        return errors

    subgraph_stack: list[tuple[int, str]] = []
    seq_stack: list[tuple[int, str]] = []

    for line_no, text in lines:
        s = text.strip()
        if not s or s.startswith("%%"):
            continue

        # Check balanced double quotes (ignore escaped \")
        q_count = len(re.findall(r'(?<!\\)"', text))
        if q_count % 2 != 0:
            errors.append(
                LintError(
                    file_path,
                    line_no,
                    "Unbalanced double quotes detected in diagram line.",
                    text,
                )
            )

        if diag_type in ("flowchart", "graph"):
            # Check edge pipe labels
            # 1. Look for unclosed edge label pipes
            if UNCLOSED_PIPE_PATTERN.search(text):
                # Count pipes following the arrow
                arrow_match = UNCLOSED_PIPE_PATTERN.search(text)
                if arrow_match:
                    after_arrow = text[arrow_match.start() :]
                    pipe_count = after_arrow.count("|")
                    if pipe_count % 2 != 0:
                        errors.append(
                            LintError(
                                file_path,
                                line_no,
                                "Unclosed edge label pipe '|' detected.",
                                text,
                            )
                        )

            # 2. Check edge label contents for unquoted delimiters
            for m in EDGE_PIPE_PATTERN.finditer(text):
                label = m.group(1).strip()
                if any(c in label for c in "()[]{}"):
                    # Delimiters must be protected by double quotes
                    if not (label.startswith('"') and label.endswith('"')):
                        errors.append(
                            LintError(
                                file_path,
                                line_no,
                                f"Edge label contains unquoted delimiters: |{label}|. "
                                f'Wrap the label in double quotes: |"{label}"|.',
                                text,
                            )
                        )

            # 3. Check node definitions for unquoted nested delimiters
            if not s.startswith(
                (
                    "subgraph",
                    "classDef",
                    "class ",
                    "style ",
                    "click ",
                    "direction ",
                    "linkStyle",
                )
            ):
                for m in re.finditer(r"\[([^\[\]\n]+)\]", text):
                    inner = m.group(1).strip()
                    if inner.startswith('"') and inner.endswith('"'):
                        continue
                    if any(c in inner for c in "()[]{}"):
                        errors.append(
                            LintError(
                                file_path,
                                line_no,
                                f"Node label contains unquoted delimiters: [{inner}]. "
                                f'Wrap the label in double quotes: ["{inner}"].',
                                text,
                            )
                        )

            # 4. Track subgraphs
            if s.startswith("subgraph"):
                subgraph_stack.append((line_no, s))
            elif s == "end":
                if not subgraph_stack:
                    errors.append(
                        LintError(
                            file_path,
                            line_no,
                            "Unexpected 'end' with no matching 'subgraph'.",
                            text,
                        )
                    )
                else:
                    subgraph_stack.pop()

        elif diag_type == "sequenceDiagram":
            if re.match(r"^(loop|alt|opt|par|critical|rect)\b", s):
                seq_stack.append((line_no, s))
            elif s == "end":
                if not seq_stack:
                    errors.append(
                        LintError(
                            file_path,
                            line_no,
                            "Unexpected 'end' with no matching sequence block.",
                            text,
                        )
                    )
                else:
                    seq_stack.pop()

    # Check for unclosed blocks
    for line_no, s in subgraph_stack:
        errors.append(
            LintError(
                file_path,
                line_no,
                f"Unclosed 'subgraph' block: '{s}'. Missing matching 'end'.",
            )
        )

    for line_no, s in seq_stack:
        errors.append(
            LintError(
                file_path,
                line_no,
                f"Unclosed sequence block: '{s}'. Missing matching 'end'.",
            )
        )

    return errors


def validate_file(file_path: str) -> list[LintError]:
    """
    Reads a Markdown file and validates all Mermaid diagram blocks.
    """
    p = Path(file_path)
    if not p.is_file():
        return [LintError(file_path, 0, f"File not found: {file_path}")]

    try:
        content = p.read_text(encoding="utf-8")
    except Exception as e:
        return [LintError(file_path, 0, f"Failed to read file: {e}")]

    blocks = extract_mermaid_blocks(content)
    errors: list[LintError] = []

    for block_start, lines in blocks:
        block_errors = validate_mermaid_block(file_path, block_start, lines)
        errors.extend(block_errors)

    return errors


def discover_markdown_files(root_dir: str = ".") -> list[str]:
    """
    Discovers all Markdown files tracked by git, or walks directory.
    """
    git_bin = shutil.which("git")
    if git_bin:
        try:
            out = subprocess.check_output(  # noqa: S603
                [git_bin, "ls-files", "*.md"],
                cwd=root_dir,
                stderr=subprocess.DEVNULL,
            ).decode("utf-8")
            files = [f.strip() for f in out.splitlines() if f.strip()]
            if files:
                return sorted(files)
        except Exception:
            pass

    # Fallback to os.walk
    ignored_dirs = {".git", "node_modules", ".venv", "venv", "__pycache__"}
    md_files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames[:] = [d for d in dirnames if d not in ignored_dirs]
        for f in filenames:
            if f.endswith(".md"):
                rel_path = os.path.relpath(os.path.join(dirpath, f), root_dir)
                md_files.append(rel_path)
    return sorted(md_files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate Mermaid diagram syntax across Markdown files."
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="Specific Markdown files to validate (default: all git-tracked .md files)",
    )
    args = parser.parse_args(argv)

    files_to_check = args.files or discover_markdown_files()
    if not files_to_check:
        print("No Markdown files found to validate.")
        return 0

    all_errors: list[LintError] = []
    total_diagrams = 0

    for file_path in files_to_check:
        try:
            content = Path(file_path).read_text(encoding="utf-8")
            blocks = extract_mermaid_blocks(content)
            total_diagrams += len(blocks)
        except Exception:
            pass

        file_errors = validate_file(file_path)
        all_errors.extend(file_errors)

    if all_errors:
        print(f"\n❌ Mermaid syntax verification failed ({len(all_errors)} error(s)):")
        for err in all_errors:
            print(f"  {err}\n")
        return 1

    print(
        f"✓ Mermaid validation passed: {total_diagrams} diagram(s) across "
        f"{len(files_to_check)} Markdown file(s) verified."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
