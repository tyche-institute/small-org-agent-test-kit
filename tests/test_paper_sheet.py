# Copyright 2026 Tyche Institute
# Licensed under the Apache License, Version 2.0; see the LICENSE file.
"""The paper sheet (the page a person fills in) says what the practice sheet (the data) says."""
import json
import re
import tomllib
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Which lines of the paper sheet each test request tries. Kept here, next to the check below, so that
# the sentence on the paper sheet cannot drift away from the requests unnoticed.
LINES_TRIED = {"R1": [2], "R2": [1], "R3": [3], "R4": [6, 7], "R5": [6, 7, 8, 9]}


def numbered(text):
    return [(int(m.group(1)), m.group(2).strip()) for m in re.finditer(r"^(\d+)\. (.+)$", text, re.M)]


def pages(pdf):
    """Number of page objects in a PDF file, read with the standard library alone."""
    data = pdf
    for stream in re.findall(rb"stream\r?\n(.*?)\r?\nendstream", pdf, re.S):
        try:
            data += b"\n" + zlib.decompress(stream)
        except zlib.error:
            pass
    return len(re.findall(rb"/Type\s*/Page(?![A-Za-z])", data))


class PaperSheet(unittest.TestCase):
    def setUp(self):
        self.data = tomllib.loads((ROOT / "sheet" / "practice-sheet.toml").read_text(encoding="utf-8"))
        self.en = (ROOT / "sheet" / "permissions-sheet.en.md").read_text(encoding="utf-8")
        self.et = (ROOT / "sheet" / "permissions-sheet.et.md").read_text(encoding="utf-8")

    def test_the_english_page_has_every_rule_in_the_words_the_agent_is_given(self):
        rules = (self.data["may"]["items"] + self.data["must_pass_to_approver"]["items"]
                 + self.data["never"]["items"])
        self.assertEqual(numbered(self.en), list(enumerate(rules, 1)))

    def test_the_estonian_page_has_the_same_9_lines_and_asks_for_a_native_reader(self):
        self.assertEqual([n for n, _ in numbered(self.et)], list(range(1, 10)))
        self.assertIn("needs native-speaker review", self.et)

    def test_the_lines_the_page_calls_tried_are_the_lines_of_the_5_requests(self):
        ids = sorted(json.loads(p.read_text(encoding="utf-8"))["id"] for p in (ROOT / "requests").glob("R*.json"))
        self.assertEqual(ids, sorted(LINES_TRIED))
        tried = sorted({n for lines in LINES_TRIED.values() for n in lines})
        not_tried = [n for n in range(1, 10) if n not in tried]
        self.assertEqual((tried, not_tried), ([1, 2, 3, 6, 7, 8, 9], [4, 5]))
        self.assertIn("tries lines 1, 2, 3, 6, 7, 8 and 9 with 5 test requests", self.en)
        self.assertIn("It does not try lines 4 and 5.", self.en)
        self.assertIn("ridu 1, 2, 3, 6, 7, 8 ja 9", self.et)
        self.assertIn("Ridu 4 ja 5 see ei proovi.", self.et)

    def test_both_pages_are_in_the_kit_as_one_page_pdf_files(self):
        for lang in ("en", "et"):
            pdf = (ROOT / "sheet" / ("permissions-sheet.%s.pdf" % lang)).read_bytes()
            self.assertTrue(pdf.startswith(b"%PDF-"), lang)
            self.assertEqual(pages(pdf), 1, lang)


if __name__ == "__main__":
    unittest.main()
