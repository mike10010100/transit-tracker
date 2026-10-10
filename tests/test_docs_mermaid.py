"""
Unit tests for Mermaid diagram syntax validation (scripts/check_mermaid.py).

Verifies that:
1. All existing Markdown diagrams in the repository are syntactically valid.
2. The validator accurately detects syntax errors, including:
   - Unquoted parentheses, brackets, and braces in flowchart edge labels (reproducing
     the exact bug: Server -->|tracker-arm (Signed Binary & Manifest)| HttpEngine).
   - Unquoted nested delimiters in node labels.
   - Unclosed edge label pipes.
   - Unclosed or mismatched subgraphs and sequence diagram blocks.
   - Unbalanced quotes and invalid diagram declarations.
3. The CLI entrypoint behaves correctly across arguments and error states.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

# Ensure repo root is on sys.path
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import check_mermaid  # noqa: E402


class TestMermaidValidator(unittest.TestCase):
    def test_all_repo_markdown_diagrams_are_valid(self):
        """All diagrams currently in the repository must pass validation."""
        files = check_mermaid.discover_markdown_files(REPO_ROOT)
        self.assertGreater(len(files), 0, "Expected at least one Markdown file in repo")

        all_errors = []
        for f in files:
            full_path = os.path.join(REPO_ROOT, f)
            errors = check_mermaid.validate_file(full_path)
            all_errors.extend(errors)

        self.assertEqual(
            len(all_errors),
            0,
            "Repository Markdown files contain Mermaid syntax errors:\n"
            + "\n".join(str(e) for e in all_errors),
        )

    def test_detects_unquoted_parens_in_flowchart_edge(self):
        """
        Reproduce the exact bug from README.md:
        Server -->|tracker-arm (Signed Binary & Manifest)| HttpEngine
        Must be rejected with a clear actionable error.
        """
        bad_diagram = (
            "```mermaid\n"
            "flowchart TD\n"
            "    Server -->|tracker-arm (Signed Binary & Manifest)| HttpEngine\n"
            "```\n"
        )
        blocks = check_mermaid.extract_mermaid_blocks(bad_diagram)
        self.assertEqual(len(blocks), 1)

        errors = check_mermaid.validate_mermaid_block("test.md", 1, blocks[0][1])
        self.assertEqual(len(errors), 1)
        self.assertIn("Edge label contains unquoted delimiters", errors[0].message)
        self.assertIn("Signed Binary & Manifest", errors[0].message)
        self.assertIn(
            'Wrap the label in double quotes: |"tracker-arm (Signed Binary & Manifest)"|',
            errors[0].message,
        )

    def test_allows_quoted_parens_in_flowchart_edge(self):
        """Quoted edge labels with parentheses must pass validation."""
        good_diagram = (
            "```mermaid\n"
            "flowchart TD\n"
            '    Server -->|"tracker-arm (Signed Binary & Manifest)"| HttpEngine\n'
            "```\n"
        )
        blocks = check_mermaid.extract_mermaid_blocks(good_diagram)
        errors = check_mermaid.validate_mermaid_block("test.md", 1, blocks[0][1])
        self.assertEqual(len(errors), 0)

    def test_detects_unquoted_brackets_and_braces_in_edge(self):
        """Square brackets and curly braces in edge labels must also require quotes."""
        cases = [
            "A -->|[label]| B",
            "A -->|{label}| B",
            "A ---|(label)| B",
            "A -.->|[nested]| B",
            "A ==>|{thick}| B",
        ]
        for c in cases:
            diagram = f"```mermaid\nflowchart TD\n    {c}\n```\n"
            blocks = check_mermaid.extract_mermaid_blocks(diagram)
            errors = check_mermaid.validate_mermaid_block("test.md", 1, blocks[0][1])
            self.assertTrue(
                any(
                    "Edge label contains unquoted delimiters" in e.message
                    for e in errors
                ),
                f"Expected error for unquoted edge: {c}",
            )

    def test_allows_quoted_brackets_and_braces_in_edge(self):
        """Quoted brackets and braces in edge labels must pass."""
        cases = [
            'A -->|"[label]"| B',
            'A -->|"{label}"| B',
            'A ---|"(label)"| B',
            'A -.->|"[nested]"| B',
            'A ==>|"{thick}"| B',
        ]
        for c in cases:
            diagram = f"```mermaid\nflowchart TD\n    {c}\n```\n"
            blocks = check_mermaid.extract_mermaid_blocks(diagram)
            errors = check_mermaid.validate_mermaid_block("test.md", 1, blocks[0][1])
            self.assertEqual(len(errors), 0, f"Unexpected error for quoted edge: {c}")

    def test_detects_unclosed_edge_label_pipe(self):
        """Unclosed pipe '|' on edge must be flagged."""
        diagram = "```mermaid\nflowchart TD\n    A -->|unclosed pipe B\n```\n"
        blocks = check_mermaid.extract_mermaid_blocks(diagram)
        errors = check_mermaid.validate_mermaid_block("test.md", 1, blocks[0][1])
        self.assertTrue(
            any("Unclosed edge label pipe" in e.message for e in errors),
            "Expected unclosed pipe error",
        )

    def test_detects_unquoted_delimiters_in_node_label(self):
        """Node definitions with unquoted nested parens or brackets must be flagged."""
        bad_node = "```mermaid\nflowchart TD\n    NJT[NJ Transit (BUSDV2 API)]\n```\n"
        blocks = check_mermaid.extract_mermaid_blocks(bad_node)
        errors = check_mermaid.validate_mermaid_block("test.md", 1, blocks[0][1])
        self.assertTrue(
            any("Node label contains unquoted delimiters" in e.message for e in errors),
            "Expected unquoted node label error",
        )

    def test_allows_quoted_delimiters_in_node_label(self):
        """Node definitions with quoted nested parens or brackets must pass."""
        good_node = (
            '```mermaid\nflowchart TD\n    NJT["NJ Transit (BUSDV2 API)"]\n```\n'
        )
        blocks = check_mermaid.extract_mermaid_blocks(good_node)
        errors = check_mermaid.validate_mermaid_block("test.md", 1, blocks[0][1])
        self.assertEqual(len(errors), 0)

    def test_subgraph_matching_and_unclosed(self):
        """Unclosed subgraphs must be detected."""
        unclosed = (
            "```mermaid\n"
            "flowchart TD\n"
            '    subgraph Cloud["External APIs"]\n'
            "        A --> B\n"
            "```\n"
        )
        blocks = check_mermaid.extract_mermaid_blocks(unclosed)
        errors = check_mermaid.validate_mermaid_block("test.md", 1, blocks[0][1])
        self.assertTrue(
            any("Unclosed 'subgraph' block" in e.message for e in errors),
            "Expected unclosed subgraph error",
        )

        unexpected_end = "```mermaid\nflowchart TD\n    A --> B\n    end\n```\n"
        blocks2 = check_mermaid.extract_mermaid_blocks(unexpected_end)
        errors2 = check_mermaid.validate_mermaid_block("test.md", 1, blocks2[0][1])
        self.assertTrue(
            any("Unexpected 'end'" in e.message for e in errors2),
            "Expected unexpected end error",
        )

    def test_sequence_diagram_validation(self):
        """Sequence diagram block matching."""
        valid_seq = (
            "```mermaid\n"
            "sequenceDiagram\n"
            "    participant A\n"
            "    participant B\n"
            "    loop Every min\n"
            "        A->>B: ping\n"
            "    end\n"
            "```\n"
        )
        blocks = check_mermaid.extract_mermaid_blocks(valid_seq)
        errors = check_mermaid.validate_mermaid_block("test.md", 1, blocks[0][1])
        self.assertEqual(len(errors), 0)

        unclosed_seq = (
            "```mermaid\n"
            "sequenceDiagram\n"
            "    loop Every min\n"
            "        A->>B: ping\n"
            "```\n"
        )
        blocks2 = check_mermaid.extract_mermaid_blocks(unclosed_seq)
        errors2 = check_mermaid.validate_mermaid_block("test.md", 1, blocks2[0][1])
        self.assertTrue(
            any("Unclosed sequence block" in e.message for e in errors2),
            "Expected unclosed loop error",
        )

        unexpected_end_seq = (
            "```mermaid\nsequenceDiagram\n    A->>B: ping\n    end\n```\n"
        )
        blocks3 = check_mermaid.extract_mermaid_blocks(unexpected_end_seq)
        errors3 = check_mermaid.validate_mermaid_block("test.md", 1, blocks3[0][1])
        self.assertTrue(
            any("Unexpected 'end'" in e.message for e in errors3),
            "Expected unexpected end in sequence diagram error",
        )

    def test_unbalanced_quotes(self):
        """Unbalanced double quotes on a line must be flagged."""
        unbalanced = '```mermaid\nflowchart TD\n    A["Unbalanced node] --> B\n```\n'
        blocks = check_mermaid.extract_mermaid_blocks(unbalanced)
        errors = check_mermaid.validate_mermaid_block("test.md", 1, blocks[0][1])
        self.assertTrue(
            any("Unbalanced double quotes" in e.message for e in errors),
            "Expected unbalanced quotes error",
        )

    def test_invalid_diagram_type(self):
        """Invalid or unknown diagram types must be flagged."""
        invalid_type = "```mermaid\nnotADiagramType TD\n    A --> B\n```\n"
        blocks = check_mermaid.extract_mermaid_blocks(invalid_type)
        errors = check_mermaid.validate_mermaid_block("test.md", 1, blocks[0][1])
        self.assertTrue(
            any(
                "Invalid or missing Mermaid diagram type declaration" in e.message
                for e in errors
            ),
            "Expected invalid diagram type declaration error",
        )

    def test_empty_block(self):
        """Empty mermaid block must be flagged."""
        empty = "```mermaid\n```\n"
        blocks = check_mermaid.extract_mermaid_blocks(empty)
        errors = check_mermaid.validate_mermaid_block("test.md", 1, blocks[0][1])
        self.assertTrue(
            any("Empty Mermaid code block" in e.message for e in errors),
            "Expected empty block error",
        )

    def test_nonexistent_file_handling(self):
        """Missing file returns error cleanly."""
        errors = check_mermaid.validate_file("does_not_exist.md")
        self.assertEqual(len(errors), 1)
        self.assertIn("File not found", errors[0].message)

    def test_cli_main_success_and_failure(self):
        """Test CLI main() behavior with valid and invalid files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Valid file
            valid_file = os.path.join(tmpdir, "valid.md")
            with open(valid_file, "w") as f:
                f.write("```mermaid\nflowchart TD\n    A --> B\n```\n")

            out = io.StringIO()
            with redirect_stdout(out):
                rc = check_mermaid.main([valid_file])
            self.assertEqual(rc, 0)
            self.assertIn("Mermaid validation passed", out.getvalue())

            # 2. Invalid file
            invalid_file = os.path.join(tmpdir, "invalid.md")
            with open(invalid_file, "w") as f:
                f.write(
                    "```mermaid\nflowchart TD\n    A -->|unquoted (parens)| B\n```\n"
                )

            out_err = io.StringIO()
            with redirect_stdout(out_err):
                rc_fail = check_mermaid.main([invalid_file])
            self.assertEqual(rc_fail, 1)
            self.assertIn("Mermaid syntax verification failed", out_err.getvalue())

            # 3. No files matching
            out_empty = io.StringIO()
            with redirect_stdout(out_empty):
                rc_none = check_mermaid.main([])
            # With empty args, it checks the real repo files which should pass
            self.assertEqual(rc_none, 0)


if __name__ == "__main__":
    unittest.main()
