"""Regression tests for parsing model FAIL/SKIP reports without inference costs.

Run: python3 -m unittest discover -s tests -p 'test_agent_verdict.py' -v
"""
import ast
from pathlib import Path
import re
import unittest


RUNNER = (Path(__file__).resolve().parents[1] /
          'engine/spore_provider_fix/work/scratch/spore_first_pass_sqlite.py')


def load_parser():
    """Load only the real parser function without importing the WSL runner."""
    source = RUNNER.read_text(encoding='utf-8')
    tree = ast.parse(source, filename=str(RUNNER))
    matches = [node for node in tree.body
               if isinstance(node, ast.FunctionDef)
               and node.name == 'parse_agent_verdict']
    if len(matches) != 1:
        raise RuntimeError('Expected exactly one parse_agent_verdict function')
    namespace = {'re': re}
    exec(compile(ast.Module(body=matches, type_ignores=[]),
                 str(RUNNER), 'exec'), namespace)
    return namespace['parse_agent_verdict']


parse = load_parser()


class AgentVerdictTests(unittest.TestCase):
    def test_original_bold_markdown_example(self):
        result = parse('**FAIL +3** — verified with ' +
                       'tools/contrib/check.py (NO MATCH, 138 bytes vs retail 144)')
        self.assertEqual(result, (
            'FAIL', '+3',
            'verified with tools/contrib/check.py (NO MATCH, 138 bytes vs retail 144)'))

    def test_heading_followed_by_bold_blocker(self):
        self.assertEqual(
            parse('## FAIL +1\n'
                  '**Verified blocker (not a codegen lever):** ABI mismatch'),
            ('FAIL', '+1', 'Verified blocker (not a codegen lever): ABI mismatch'))

    def test_plain_text_colon(self):
        self.assertEqual(parse('FAIL +2: mismatched register allocation'),
                         ('FAIL', '+2', 'mismatched register allocation'))

    def test_bold_colon_inside_formatting(self):
        self.assertEqual(parse('**FAIL +3:** wrong binary size'),
                         ('FAIL', '+3', 'wrong binary size'))

    def test_markdown_heading_with_em_dash(self):
        self.assertEqual(parse('### FAIL +4 — incorrect stack layout'),
                         ('FAIL', '+4', 'incorrect stack layout'))

    def test_bulleted_verdict(self):
        self.assertEqual(parse('- **FAIL +5**: x87 vs SSE code generation'),
                         ('FAIL', '+5', 'x87 vs SSE code generation'))

    def test_case_insensitive_and_extra_spaces(self):
        self.assertEqual(parse('fail  + 2 : wrong flags'),
                         ('FAIL', '+2', 'wrong flags'))

    def test_plain_skip(self):
        self.assertEqual(parse('SKIP: requires shared header changes'),
                         ('SKIP', '', 'requires shared header changes'))

    def test_formatted_skip(self):
        self.assertEqual(parse('**SKIP** — shared header must change'),
                         ('SKIP', '', 'shared header must change'))

    def test_inline_code_verdict(self):
        self.assertEqual(parse(chr(96) + 'FAIL +2' + chr(96) +
                               ': compiler refused ABI'),
                         ('FAIL', '+2', 'compiler refused ABI'))

    def test_no_explicit_verdict(self):
        self.assertIsNone(parse('Could not match the retail bytes, 138 vs 144.'))

    def test_plain_match_never_counts_as_verified(self):
        self.assertIsNone(parse('MATCH: yes, I matched it'))

    def test_bold_match_never_counts_as_verified(self):
        self.assertIsNone(parse('**MATCH:** independently verified (model says so)'))

    def test_fail_in_a_sentence_does_not_count(self):
        self.assertIsNone(parse('This function might FAIL +3 during compilation.'))

    def test_quoted_failure_is_not_final_verdict(self):
        self.assertIsNone(parse('> FAIL +2: previous attempt'))

    def test_code_fence_does_not_override_actual_verdict(self):
        fence = chr(96) * 3
        self.assertEqual(parse(
            'Example:\n' + fence + 'text\nFAIL +2: old example\n' +
            fence + '\n**FAIL +4** — final technical blocker'),
            ('FAIL', '+4', 'final technical blocker'))

    def test_invalid_effort_value_does_not_count(self):
        self.assertIsNone(parse('FAIL +9: out-of-range'))

    def test_empty_blocker_does_not_crash(self):
        self.assertEqual(parse('**FAIL +3**'), ('FAIL', '+3', 'No reason supplied'))

    def test_keeps_checker_verification_in_control(self):
        source = RUNNER.read_text(encoding='utf-8')
        self.assertIn('is_match, check_note = checked_result(va, log)', source)
        self.assertIn('verdict = parse_agent_verdict(answer)', source)
        self.assertIn("if is_match:\n                    outcome = ('MATCH'", source)


if __name__ == '__main__':
    unittest.main()
