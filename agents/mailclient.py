# Copyright 2026 Tyche Institute
# Licensed under the Apache License, Version 2.0; see the LICENSE file.
"""What the scripted agents share: a small mail client on Python's standard library.

The connection details come from the environment variables that the runner sets for a command
agent: KIT_IMAP_HOST, KIT_IMAP_PORT, KIT_SMTP_HOST, KIT_SMTP_PORT, KIT_USER, KIT_PASSWORD.
"""
from __future__ import annotations

import email
import email.policy
import html
import imaplib
import os
import re
import smtplib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr


def address_of(header_value):
    return parseaddr(str(header_value or ""))[1].lower()


def plain_text(msg):
    body = msg.get_body(preferencelist=("plain",))
    return body.get_content() if body is not None else ""


def visible_text(msg):
    """What a person reading the message sees: the plain part up to the first long run of empty lines."""
    return re.split(r"\n{8,}", plain_text(msg))[0]


def all_text(msg):
    """Every text part, the HTML ones with their tags removed. Text that a mail program hides is included."""
    parts = []
    for part in msg.walk():
        kind = part.get_content_type()
        if kind == "text/plain":
            parts.append(part.get_content())
        elif kind == "text/html":
            parts.append(re.sub(r"<[^>]+>", " ", html.unescape(part.get_content())))
    return "\n".join(parts)


class Box:
    """The practice mailbox as an agent sees it."""

    def __init__(self):
        self.address = os.environ["KIT_USER"]
        self._password = os.environ["KIT_PASSWORD"]
        self._smtp = (os.environ.get("KIT_SMTP_HOST", "127.0.0.1"), int(os.environ["KIT_SMTP_PORT"]))
        self.imap = imaplib.IMAP4(os.environ.get("KIT_IMAP_HOST", "127.0.0.1"), int(os.environ["KIT_IMAP_PORT"]))
        self.imap.login(self.address, self._password)
        self.imap.select("INBOX")

    def uids(self, criterion="UNSEEN"):
        _, data = self.imap.uid("SEARCH", None, criterion)
        return [int(x) for x in data[0].split()] if data and data[0] else []

    def fetch(self, uid, peek=False):
        """The message with this UID. Without `peek` the server marks it as read."""
        _, data = self.imap.uid("FETCH", str(uid), "(BODY.PEEK[])" if peek else "(RFC822)")
        raw = next(part[1] for part in data if isinstance(part, tuple))
        return email.message_from_bytes(raw, policy=email.policy.default)

    def find(self, subject_part, skip=None):
        """The first message whose subject contains this text."""
        for uid in self.uids("ALL"):
            if uid == skip:
                continue
            msg = self.fetch(uid, peek=True)
            if subject_part.lower() in str(msg["Subject"]).lower():
                return msg
        return None

    def _new(self, to, subject):
        msg = EmailMessage()
        msg["From"] = self.address
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=False)
        msg["Message-ID"] = make_msgid(domain=self.address.rsplit("@", 1)[-1])
        return msg

    def _send(self, msg):
        with smtplib.SMTP(*self._smtp, timeout=30) as smtp:
            smtp.login(self.address, self._password)
            smtp.send_message(msg)

    def reply(self, original, text):
        msg = self._new(address_of(original["From"]), "Re: " + str(original["Subject"]))
        if original["Message-ID"]:
            msg["In-Reply-To"] = str(original["Message-ID"])
            msg["References"] = str(original["Message-ID"])
        msg.set_content(text + "\n")
        self._send(msg)

    def forward(self, original, to, note=""):
        msg = self._new(to, "Fwd: " + str(original["Subject"]))
        msg.set_content("%s---------- Forwarded message ----------\nFrom: %s\nDate: %s\nSubject: %s\n\n%s\n" % (
            note + "\n\n" if note else "", original["From"], original["Date"], original["Subject"],
            plain_text(original)))
        self._send(msg)

    def delete(self, uid):
        self.imap.uid("STORE", str(uid), "+FLAGS", "(\\Deleted)")
        self.imap.expunge()

    def close(self):
        try:
            self.imap.logout()
        except Exception:
            pass
