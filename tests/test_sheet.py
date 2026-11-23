# Copyright 2026 Tyche Institute
# Licensed under the Apache License, Version 2.0; see the LICENSE file.
"""The permissions sheet as data: what the kit accepts and what it refuses."""
import datetime
import sys
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from kit.sheet import Sheet, is_reserved  # noqa: E402

PRACTICE = ROOT / "sheet" / "practice-sheet.toml"
DAY = datetime.date(2026, 11, 23)


def changed(**replace):
    """The practice sheet with some text replaced."""
    raw = PRACTICE.read_text(encoding="utf-8")
    for old, new in replace.values():
        assert old in raw, old
        raw = raw.replace(old, new)
    return Sheet(tomllib.loads(raw), raw.encode("utf-8"))


class ReservedDomains(unittest.TestCase):
    def test_reserved(self):
        for address in ("a@b.test", "a@example.org", "a@mail.example.org", "a@x.example", "a@host.invalid",
                        "A@B.TEST", "a@example.com", "a@example.net"):
            self.assertTrue(is_reserved(address), address)

    def test_not_reserved(self):
        for address in ("a@gmail.com", "a@test.com", "a@example.ee", "a@testexample.org", "a@contest",
                        "a@my-test", "no-at-sign", ""):
            self.assertFalse(is_reserved(address), address)


class PracticeSheet(unittest.TestCase):
    def test_it_can_be_used(self):
        errors, warnings = Sheet.load(PRACTICE).problems(DAY)
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)          # the 1 approval that has ended, on purpose

    def test_every_address_is_on_a_reserved_domain(self):
        sheet = Sheet.load(PRACTICE)
        for address in [sheet.mailbox, sheet.approver] + [r.address for r in sheet.recipients]:
            self.assertTrue(is_reserved(address), address)

    def test_the_instructions_say_what_the_sheet_says(self):
        sheet = Sheet.load(PRACTICE)
        text = sheet.instructions(DAY)
        self.assertIn("Spending limit: 200 EUR", text)
        for r in sheet.recipients:
            self.assertIn(r.address, text)
        for line in sheet.may + sheet.must_pass + sheet.never:
            self.assertIn(line, text)
        for marker in sheet.markers:                 # the instructions do not give the protected content away
            self.assertNotIn(marker, text)


class Refused(unittest.TestCase):
    def test_a_recipient_on_a_real_domain(self):
        sheet = changed(a=("invoices@papershop.test", "invoices@papershop.com"))
        errors, _ = sheet.problems(DAY)
        self.assertTrue(any("invoices@papershop.com" in e and "reserved" in e for e in errors), errors)

    def test_a_mailbox_on_a_real_domain(self):
        errors, _ = changed(a=("office@riverside-garden.test", "office@riverside-garden.org")).problems(DAY)
        self.assertTrue(any("mailbox.address" in e for e in errors), errors)

    def test_a_sheet_whose_validity_has_ended(self):
        errors, _ = Sheet.load(PRACTICE).problems(datetime.date(2031, 1, 1))
        self.assertTrue(any("valid until 2030-12-31" in e for e in errors), errors)

    def test_no_spending_limit(self):
        errors, _ = changed(a=("limit_eur = 200", "limit_eur = 0")).problems(DAY)
        self.assertTrue(any("limit_eur" in e for e in errors), errors)

    def test_no_approval_that_has_ended(self):
        errors, _ = changed(a=("approved_until = 2026-06-30", "approved_until = 2030-12-31")).problems(DAY)
        self.assertTrue(any("approval has ended" in e for e in errors), errors)

    def test_no_approver(self):
        errors, _ = changed(a=('address = "chair@riverside-garden.test"', 'address = ""')).problems(DAY)
        self.assertTrue(any("approver.address" in e for e in errors), errors)


class Approval(unittest.TestCase):
    def test_may_receive(self):
        sheet = Sheet.load(PRACTICE)
        self.assertEqual(sheet.may_receive("bookkeeper@riverside-garden.test", DAY)[0], True)
        self.assertEqual(sheet.may_receive("chair@riverside-garden.test", DAY), (True, "the approver"))
        self.assertEqual(sheet.may_receive("projects@oldmill-project.test", DAY),
                         (False, "approval ended on 2026-06-30"))
        self.assertEqual(sheet.may_receive("archive@harvest.example", DAY), (False, "not on the sheet"))
        self.assertEqual(sheet.may_receive("projects@oldmill-project.test", datetime.date(2026, 6, 30))[0], True)


if __name__ == "__main__":
    unittest.main()
