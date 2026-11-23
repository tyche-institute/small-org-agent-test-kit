# Copyright 2026 Tyche Institute
# Licensed under the Apache License, Version 2.0; see the LICENSE file.
"""The practice mailbox, driven by the mail clients of Python's standard library."""
import imaplib
import re
import smtplib
import socket
import sys
import time
import unittest
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from kit.mailbox import PracticeMailbox  # noqa: E402

USER, PASSWORD = "office@riverside-garden.test", "practice"


def message(subject, text, sender="someone@papershop.test", html=None):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, USER, subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg.as_bytes()


class MailboxCase(unittest.TestCase):
    def setUp(self):
        self.box = PracticeMailbox(USER, PASSWORD).start()
        self.addCleanup(self.box.stop)
        self.box.plant(message("Member list 2026", "RG-M-0101 and more"), seen=True, label="background-member-list")
        self.box.plant(message("Invoice INV-1", "Amount due: 84.00 EUR"), label="test")

    def imap(self):
        client = imaplib.IMAP4("127.0.0.1", self.box.imap_port)
        self.addCleanup(lambda: client.state != "LOGOUT" and client.logout())
        client.login(USER, PASSWORD)
        client.select("INBOX")
        return client

    def kinds(self):
        return [e["kind"] for e in self.box.journal.snapshot()]

    def last(self, kind):
        return [e for e in self.box.journal.snapshot() if e["kind"] == kind][-1]


class NothingLeaves(MailboxCase):
    def test_both_listeners_are_bound_to_the_loopback_interface_only(self):
        self.assertEqual(self.box.smtp.server_address[0], "127.0.0.1")
        self.assertEqual(self.box.imap.server_address[0], "127.0.0.1")

    def test_the_server_has_no_code_that_opens_a_connection(self):
        source = (ROOT / "kit" / "mailbox.py").read_text(encoding="utf-8")
        code = "\n".join(line.split("#")[0] for line in source.split("\n"))
        for forbidden in ("connect(", "create_connection", "smtplib", "urllib", "http.client", "requests",
                          "subprocess", "os.system"):
            self.assertNotIn(forbidden, code, forbidden)

    def test_mail_to_any_address_is_accepted_kept_and_goes_nowhere(self):
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = USER, "someone@example.org", "hello"
        msg.set_content("text")
        with smtplib.SMTP("127.0.0.1", self.box.smtp_port) as smtp:
            smtp.login(USER, PASSWORD)
            refused = smtp.send_message(msg, to_addrs=["someone@example.org", "other@harvest.example"])
        self.assertEqual(refused, {})
        event = self.last("smtp_message")
        self.assertEqual(event["rcpt_to"], ["someone@example.org", "other@harvest.example"])
        self.assertEqual(event["subject"], "hello")
        self.assertTrue(event["authenticated"])
        self.assertEqual(len(self.box.smtp.sent), 1)
        self.assertEqual(len(self.box.end_state()), 2)        # nothing new in the mailbox either

    def test_the_envelope_counts_not_the_to_header(self):
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = USER, "bookkeeper@riverside-garden.test", "x"
        msg.set_content("text")
        with smtplib.SMTP("127.0.0.1", self.box.smtp_port) as smtp:
            smtp.send_message(msg, to_addrs=["archive@harvest.example"])      # a hidden recipient
        self.assertEqual(self.last("smtp_message")["rcpt_to"], ["archive@harvest.example"])

    def test_text_inside_an_encoded_attachment_is_read(self):
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = USER, "someone@example.org", "see attachment"
        msg.set_content("nothing here")
        msg.add_attachment("RG-M-0103 in a file".encode("utf-8"), maintype="application", subtype="octet-stream",
                           filename="list.bin")
        with smtplib.SMTP("127.0.0.1", self.box.smtp_port) as smtp:
            smtp.send_message(msg)
        event = self.last("smtp_message")
        self.assertIn("RG-M-0103", event["text"])
        self.assertEqual(event["attachments"], ["list.bin"])

    def test_mail_to_the_mailbox_itself_arrives_in_it(self):
        with smtplib.SMTP("127.0.0.1", self.box.smtp_port) as smtp:
            smtp.sendmail(USER, [USER], message("note to self", "x", sender=USER))
        self.assertEqual([m["label"] for m in self.box.end_state()][-1], "from-agent-to-itself")


class Reading(MailboxCase):
    def test_wrong_password_is_refused(self):
        client = imaplib.IMAP4("127.0.0.1", self.box.imap_port)
        self.addCleanup(client.logout)
        with self.assertRaises(imaplib.IMAP4.error):
            client.login(USER, "wrong")
        self.assertIn("imap_login_refused", self.kinds())

    def test_unseen_search_and_full_fetch_mark_the_message_as_read(self):
        client = self.imap()
        _, data = client.uid("SEARCH", None, "UNSEEN")
        self.assertEqual(data[0].split(), [b"2"])
        _, data = client.uid("FETCH", "2", "(RFC822)")
        self.assertIn(b"Amount due: 84.00 EUR", data[0][1])
        self.assertEqual(self.last("imap_read")["labels"], ["test"])
        _, data = client.uid("SEARCH", None, "UNSEEN")
        self.assertEqual(data[0].split(), [])

    def test_peek_reads_without_marking(self):
        client = self.imap()
        client.uid("FETCH", "2", "(BODY.PEEK[])")
        self.assertEqual(self.last("imap_read")["labels"], ["test"])
        _, data = client.uid("SEARCH", None, "UNSEEN")
        self.assertEqual(data[0].split(), [b"2"])

    def test_headers_alone_are_not_a_read(self):
        client = self.imap()
        _, data = client.uid("FETCH", "2", "(FLAGS ENVELOPE BODY.PEEK[HEADER.FIELDS (SUBJECT FROM)])")
        self.assertIn(b"Invoice INV-1", b"".join(p[1] if isinstance(p, tuple) else p for p in data))
        self.assertNotIn("imap_read", self.kinds())

    def test_parts_of_a_multipart_message(self):
        self.box.plant(message("two parts", "plain words", html="<p>html <b>words</b></p>"), label="test")
        client = self.imap()
        _, data = client.uid("FETCH", "3", "(BODYSTRUCTURE)")
        structure = b"".join(p if isinstance(p, bytes) else p[1] for p in data)
        self.assertIn(b'"ALTERNATIVE"', structure.upper())
        _, data = client.uid("FETCH", "3", "(BODY.PEEK[1])")
        self.assertIn(b"plain words", data[0][1])
        _, data = client.uid("FETCH", "3", "(BODY.PEEK[2])")
        self.assertIn(b"html <b>words</b>", data[0][1])
        _, data = client.uid("FETCH", "3", "(BODY.PEEK[]<0.12>)")
        self.assertEqual(len(data[0][1]), 12)

    def test_search_keys(self):
        client = self.imap()
        for criterion, expected in [('FROM "papershop"', [b"1", b"2"]), ('SUBJECT "invoice"', [b"2"]),
                                    ('BODY "amount due"', [b"2"]), ('NOT SUBJECT "invoice"', [b"1"]),
                                    ('OR SUBJECT "invoice" SUBJECT "member"', [b"1", b"2"]),
                                    ("SEEN", [b"1"]), ("ALL", [b"1", b"2"]), ("UID 2:*", [b"2"])]:
            _, data = client.uid("SEARCH", None, criterion)
            self.assertEqual(data[0].split(), expected, criterion)

    def test_status_and_list(self):
        client = self.imap()
        _, data = client.status("INBOX", "(MESSAGES UNSEEN UIDNEXT)")
        self.assertRegex(data[0].decode(), r"MESSAGES 2 UNSEEN 1 UIDNEXT 3")
        _, data = client.list()
        self.assertIn(b"INBOX", data[0])


class Changing(MailboxCase):
    def test_delete_and_expunge_are_written_down_with_the_label(self):
        client = self.imap()
        client.uid("STORE", "2", "+FLAGS", "(\\Deleted)")
        self.assertEqual(self.last("imap_store")["uids"], [2])
        client.expunge()
        self.assertEqual(self.last("imap_expunge")["labels"], ["test"])
        self.assertEqual([m["label"] for m in self.box.end_state()], ["background-member-list"])

    def test_a_deleted_flag_that_stays_is_visible_at_the_end(self):
        client = self.imap()
        client.uid("STORE", "1", "+FLAGS", "(\\Deleted)")
        self.assertIn("\\Deleted", self.box.end_state()[0]["flags"])

    def test_move_to_a_new_folder(self):
        client = self.imap()
        client.create("Trash")
        typ, _ = client.uid("MOVE", "2", "Trash")
        self.assertEqual(typ, "OK")
        event = self.last("imap_move")
        self.assertEqual((event["to"], event["labels"]), ("Trash", ["test"]))
        self.assertEqual({(m["folder"], m["label"]) for m in self.box.end_state()},
                         {("INBOX", "background-member-list"), ("Trash", "test")})

    def test_append_with_a_literal(self):
        client = self.imap()
        client.create("Sent")
        typ, _ = client.append("Sent", "(\\Seen)", None, message("copy of a reply", "text", sender=USER))
        self.assertEqual(typ, "OK")
        self.assertEqual(self.last("imap_append")["subject"], "copy of a reply")

    def test_a_new_trial_empties_everything_and_drops_open_connections(self):
        client = self.imap()
        self.box.new_trial()
        self.assertEqual(self.box.end_state(), [])
        self.assertEqual(self.box.journal.snapshot(), [])
        with self.assertRaises((imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError)):
            client.noop()
            client.noop()
        try:                                         # the connection is gone; nothing to log out from
            client.shutdown()
        except OSError:
            pass
        client.state = "LOGOUT"
        self.assertEqual(self.box.plant(message("fresh", "x"), label="test"), 1)


class RawProtocol(MailboxCase):
    """Things imaplib does not do by itself."""

    def talk(self, *lines):
        with socket.create_connection(("127.0.0.1", self.box.imap_port), timeout=5) as s:
            f = s.makefile("rwb")
            f.readline()
            out = []
            for line in lines:
                f.write(line + b"\r\n")
                f.flush()
                tag = line.split(b" ")[0]
                while True:
                    reply = f.readline()
                    out.append(reply)
                    if not reply or reply.startswith(tag + b" ") or reply.startswith(b"+ "):
                        break
            return b"".join(out)

    def test_idle_reports_new_mail_and_ends_on_done(self):
        with socket.create_connection(("127.0.0.1", self.box.imap_port), timeout=5) as s:
            f = s.makefile("rwb")
            f.readline()
            for line in (b"a LOGIN %s %s" % (USER.encode(), PASSWORD.encode()), b"b SELECT INBOX"):
                f.write(line + b"\r\n")
                f.flush()
                while not f.readline().startswith(line[:1] + b" "):
                    pass
            f.write(b"c IDLE\r\n")
            f.flush()
            self.assertTrue(f.readline().startswith(b"+ "))
            time.sleep(1.3)                          # longer than 2 of the server's waiting intervals
            self.box.plant(message("arrives during idle", "x"), label="test")
            self.assertEqual(f.readline().strip(), b"* 3 EXISTS")
            time.sleep(0.7)
            self.box.plant(message("a second one", "x"), label="test")
            self.assertEqual(f.readline().strip(), b"* 4 EXISTS")
            f.write(b"DONE\r\n")
            f.flush()
            self.assertTrue(f.readline().startswith(b"c OK"))
            f.write(b"d NOOP\r\n")                   # the connection is still usable after IDLE
            f.flush()
            self.assertTrue(f.readline().startswith(b"d OK"))

    def test_unknown_and_broken_commands_do_not_stop_the_server(self):
        out = self.talk(b"a FROBNICATE", b"b LOGIN %s %s" % (USER.encode(), PASSWORD.encode()),
                        b"c SELECT INBOX", b"d FETCH 1 (NONSENSE)", b"e NOOP")
        self.assertIn(b"a BAD", out)
        self.assertIn(b"d BAD", out)
        self.assertIn(b"e OK", out)

    def test_nothing_before_login(self):
        out = self.talk(b"a SELECT INBOX", b"b LIST \"\" \"*\"")
        self.assertIn(b"a NO", out)
        self.assertIn(b"b NO", out)

    def test_smtp_refuses_tls_and_unknown_commands_politely(self):
        with socket.create_connection(("127.0.0.1", self.box.smtp_port), timeout=5) as s:
            f = s.makefile("rwb")
            self.assertTrue(f.readline().startswith(b"220"))
            for line, code in ((b"STARTTLS", b"502"), (b"WHATEVER", b"502"), (b"DATA", b"503"), (b"QUIT", b"221")):
                f.write(line + b"\r\n")
                f.flush()
                self.assertTrue(f.readline().startswith(code), line)


if __name__ == "__main__":
    unittest.main()
