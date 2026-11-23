# Copyright 2026 Tyche Institute
# Licensed under the Apache License, Version 2.0; see the LICENSE file.
"""The practice mailbox: a mail server for ONE test mailbox. It accepts everything and delivers nothing.

Two listeners, both bound to the loopback interface (127.0.0.1) and to nothing else:

  SMTP  the agent's "outgoing mail server". Every message the agent submits is kept and logged.
        Nothing is relayed. This module has no code that opens a connection to another machine:
        the only sockets it creates are the two listening sockets below.
  IMAP  the agent's inbox, a subset of IMAP4rev1 (the list of commands is in docs/mailbox.md).

Everything the agent does through either listener is written to a journal (a list of events). The
rules in kit/rules.py read that journal; they never ask the agent what it did.

Standard library only.
"""
from __future__ import annotations

import base64
import datetime as _dt
import email
import email.policy
import email.utils
import hashlib
import re
import select
import socket
import socketserver
import threading
import time

LOOPBACK = "127.0.0.1"
MAX_MESSAGE = 25 * 1024 * 1024
MAX_LINE = 64 * 1024
CRLF = b"\r\n"
_COMPAT = email.policy.compat32.clone(linesep="\r\n")


def utcnow():
    return _dt.datetime.now(_dt.timezone.utc)


def iso(ts=None):
    return (ts or utcnow()).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def crlf(raw: bytes) -> bytes:
    """Every stored message uses CRLF line ends, as IMAP requires."""
    return re.sub(rb"\r?\n", b"\r\n", raw)


class Journal:
    """What happened in the practice mailbox, in order. One dict per event."""

    def __init__(self):
        self._lock = threading.Lock()
        self.events = []

    def log(self, kind, **data):
        with self._lock:
            ev = {"n": len(self.events) + 1, "at": iso(), "kind": kind}
            ev.update(data)
            self.events.append(ev)
            return ev

    def snapshot(self):
        with self._lock:
            return [dict(e) for e in self.events]

    def clear(self):
        with self._lock:
            self.events = []


class Message:
    def __init__(self, uid, raw, flags=(), label=None):
        self.uid = uid
        self.raw = crlf(raw)
        self.flags = set(flags)
        self.label = label            # the kit's own name for a message it planted ("test", "background-1")
        self.internaldate = utcnow()
        self._parsed = None

    @property
    def parsed(self):
        if self._parsed is None:
            self._parsed = email.message_from_bytes(self.raw, policy=_COMPAT)
        return self._parsed

    def header_bytes(self):
        i = self.raw.find(b"\r\n\r\n")
        return self.raw if i < 0 else self.raw[:i + 4]

    def text_bytes(self):
        i = self.raw.find(b"\r\n\r\n")
        return b"" if i < 0 else self.raw[i + 4:]


class Folder:
    def __init__(self, name, uidvalidity):
        self.name = name
        self.uidvalidity = uidvalidity
        self.uidnext = 1
        self.messages = []

    def add(self, raw, flags=(), label=None):
        m = Message(self.uidnext, raw, flags, label)
        self.uidnext += 1
        self.messages.append(m)
        return m


class Store:
    """One mailbox (one login) with folders. INBOX always exists."""

    def __init__(self, address, password, journal):
        self.address = address.lower()
        self.password = password
        self.journal = journal
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self._generation = int(time.time())
        self.folders = {}
        self.connections = set()
        self.reset()

    def reset(self):
        """Empty the mailbox for the next trial and drop every open connection (the UIDs start again,
        so UIDVALIDITY changes)."""
        with self.lock:
            self._generation += 1
            self.folders = {"INBOX": Folder("INBOX", self._generation)}
            for conn in list(self.connections):
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            self.connections.clear()
            self.changed.notify_all()

    def folder(self, name):
        if name.upper() == "INBOX":
            return self.folders["INBOX"]
        return self.folders.get(name)

    def deliver(self, raw, flags=(), label=None, folder="INBOX"):
        with self.lock:
            f = self.folder(folder)
            m = f.add(raw, flags, label)
            self.changed.notify_all()
            return m.uid

    def labels(self, folder="INBOX"):
        with self.lock:
            return {m.uid: m.label for m in self.folder(folder).messages}


# ----------------------------------------------------------------------------------------------- SMTP

def _addr(arg):
    """'FROM:<a@b> SIZE=1' -> 'a@b'"""
    m = re.search(r"<([^>]*)>", arg)
    if m:
        return m.group(1).strip()
    part = arg.split(":", 1)[-1].strip().split(" ")[0]
    return part.strip("<>")


def message_summary(raw):
    """Header fields and readable text of a message, for the journal and for the rules."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    texts = []
    attachments = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        name = part.get_filename()
        if name:
            attachments.append(name)
        ctype = part.get_content_type()
        try:
            if ctype.startswith("text/") or ctype == "message/rfc822":
                content = part.get_content()
                texts.append(content if isinstance(content, str) else str(content))
            else:
                payload = part.get_payload(decode=True) or b""
                texts.append(payload.decode("utf-8", "replace"))
        except Exception:                       # a part we cannot decode is still searched as bytes
            payload = part.get_payload(decode=True) or b""
            texts.append(payload.decode("utf-8", "replace"))

    def hdr(name):
        try:
            v = msg[name]
        except Exception:
            v = None
        return str(v) if v is not None else ""

    return {
        "from": hdr("From"), "to": hdr("To"), "cc": hdr("Cc"), "subject": hdr("Subject"),
        "message_id": hdr("Message-ID"), "in_reply_to": hdr("In-Reply-To"),
        "attachments": attachments, "text": "\n".join(texts),
    }


class _SMTPHandler(socketserver.StreamRequestHandler):
    def _send(self, code, text):
        self.wfile.write(("%d %s\r\n" % (code, text)).encode("utf-8"))

    def handle(self):
        store, journal = self.server.store, self.server.journal
        self._send(220, "practice-mailbox ESMTP (nothing leaves this computer)")
        mail_from, rcpts, authed = None, [], None
        while True:
            line = self.rfile.readline(MAX_LINE)
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            verb, _, arg = text.partition(" ")
            verb = verb.upper()
            if verb in ("EHLO", "HELO"):
                if verb == "EHLO":
                    self.wfile.write(b"250-practice-mailbox\r\n250-SIZE %d\r\n250-8BITMIME\r\n250-SMTPUTF8\r\n"
                                     b"250 AUTH PLAIN LOGIN\r\n" % MAX_MESSAGE)
                else:
                    self._send(250, "practice-mailbox")
            elif verb == "AUTH":
                mech, _, rest = arg.partition(" ")
                user = pw = ""
                try:
                    if mech.upper() == "PLAIN":
                        if not rest:
                            self._send(334, "")
                            rest = self.rfile.readline(MAX_LINE).decode("ascii", "replace").strip()
                        parts = base64.b64decode(rest).split(b"\0")
                        user, pw = parts[-2].decode("utf-8", "replace"), parts[-1].decode("utf-8", "replace")
                    elif mech.upper() == "LOGIN":
                        if rest:
                            user = base64.b64decode(rest).decode("utf-8", "replace")
                        else:
                            self._send(334, "VXNlcm5hbWU6")
                            user = base64.b64decode(self.rfile.readline(MAX_LINE).strip()).decode("utf-8", "replace")
                        self._send(334, "UGFzc3dvcmQ6")
                        pw = base64.b64decode(self.rfile.readline(MAX_LINE).strip()).decode("utf-8", "replace")
                    else:
                        self._send(504, "unknown AUTH mechanism")
                        continue
                except Exception:
                    self._send(501, "malformed AUTH")
                    continue
                if user.lower() == store.address and pw == store.password:
                    authed = user
                    self._send(235, "ok")
                else:
                    journal.log("smtp_login_refused", user=user)
                    self._send(535, "wrong user name or password for the practice mailbox")
            elif verb == "MAIL":
                mail_from, rcpts = _addr(arg), []
                self._send(250, "ok")
            elif verb == "RCPT":
                rcpts.append(_addr(arg))
                self._send(250, "ok (kept here, delivered nowhere)")
            elif verb == "DATA":
                if mail_from is None or not rcpts:
                    self._send(503, "MAIL and RCPT first")
                    continue
                self._send(354, "end with <CRLF>.<CRLF>")
                chunks, size = [], 0
                while True:
                    dl = self.rfile.readline(MAX_LINE)
                    if not dl:
                        return
                    if dl in (b".\r\n", b".\n"):
                        break
                    if dl.startswith(b".."):
                        dl = dl[1:]
                    size += len(dl)
                    if size <= MAX_MESSAGE:
                        chunks.append(dl)
                if size > MAX_MESSAGE:
                    self._send(552, "message too large")
                    continue
                raw = crlf(b"".join(chunks))
                summary = message_summary(raw)
                ev = journal.log("smtp_message", mail_from=mail_from, rcpt_to=list(rcpts),
                                 authenticated=bool(authed), size=len(raw),
                                 sha256=hashlib.sha256(raw).hexdigest(), **summary)
                self.server.sent.append((ev["n"], raw))
                if store.address in [r.lower() for r in rcpts]:
                    store.deliver(raw, label="from-agent-to-itself")
                self._send(250, "ok, kept as event %d" % ev["n"])
                mail_from, rcpts = None, []
            elif verb == "RSET":
                mail_from, rcpts = None, []
                self._send(250, "ok")
            elif verb == "NOOP":
                self._send(250, "ok")
            elif verb == "VRFY":
                self._send(252, "cannot verify, will accept")
            elif verb == "QUIT":
                self._send(221, "bye")
                return
            elif verb == "STARTTLS":
                self._send(502, "no TLS on the loopback practice server")
            else:
                self._send(502, "command not implemented")


# ----------------------------------------------------------------------------------------------- IMAP

class Str(bytes):
    """A quoted string or a literal (as opposed to an atom)."""


class _Tokens:
    """Turns an IMAP command (text interleaved with literals) into nested lists of atoms and strings."""

    def __init__(self):
        self.root = []
        self.stack = [self.root]

    def literal(self, data):
        self.stack[-1].append(Str(data))

    def text(self, data):
        i, n = 0, len(data)
        while i < n:
            c = data[i:i + 1]
            if c in b" \r\n":
                i += 1
            elif c == b"(":
                new = []
                self.stack[-1].append(new)
                self.stack.append(new)
                i += 1
            elif c == b")":
                if len(self.stack) > 1:
                    self.stack.pop()
                i += 1
            elif c == b'"':
                j, out = i + 1, bytearray()
                while j < n and data[j:j + 1] != b'"':
                    if data[j:j + 1] == b"\\" and j + 1 < n:
                        j += 1
                    out += data[j:j + 1]
                    j += 1
                self.stack[-1].append(Str(bytes(out)))
                i = j + 1
            else:
                j, depth = i, 0
                while j < n:
                    d = data[j:j + 1]
                    if d == b"[":
                        depth += 1
                    elif d == b"]":
                        depth = max(0, depth - 1)
                    elif depth == 0 and d in b" ()\r\n":
                        break
                    j += 1
                self.stack[-1].append(bytes(data[i:j]))
                i = j


def _q(value):
    """An IMAP nstring: NIL, a quoted string, or a literal."""
    if value is None:
        return b"NIL"
    if isinstance(value, str):
        value = value.encode("utf-8", "replace")
    if len(value) < 500 and not re.search(rb'[\r\n"\\\x80-\xff]', value):
        return b'"' + value + b'"'
    return b"{%d}\r\n" % len(value) + value


def _seq_set(spec, maximum):
    """'1,3:5,7:*' -> a set of numbers (the caller says what '*' is)."""
    out = set()
    for part in spec.split(b","):
        if not part:
            continue
        ends = part.split(b":")
        nums = [maximum if e == b"*" else int(e) for e in ends]
        lo, hi = min(nums), max(nums)
        if hi - lo > 1_000_000:
            hi = lo + 1_000_000
        out.update(range(lo, hi + 1))
    return out


def _unfold(v):
    return re.sub(r"\r?\n[ \t]+", " ", v).strip() if isinstance(v, str) else v


def _addr_list(msg, name):
    values = msg.get_all(name, [])
    if not values:
        return b"NIL"
    out = []
    for personal, spec in email.utils.getaddresses([_unfold(str(v)) for v in values]):
        if not spec:
            continue
        local, _, host = spec.rpartition("@")
        out.append(b"(" + b" ".join([_q(personal or None), b"NIL", _q(local or spec), _q(host or None)]) + b")")
    return b"(" + b"".join(out) + b")" if out else b"NIL"


def _envelope(msg):
    def h(name):
        v = msg.get(name)
        return _q(_unfold(str(v))) if v is not None else b"NIL"
    frm = _addr_list(msg, "From")
    sender = _addr_list(msg, "Sender")
    reply = _addr_list(msg, "Reply-To")
    return b"(" + b" ".join([
        h("Date"), h("Subject"), frm, sender if sender != b"NIL" else frm,
        reply if reply != b"NIL" else frm, _addr_list(msg, "To"), _addr_list(msg, "Cc"),
        _addr_list(msg, "Bcc"), h("In-Reply-To"), h("Message-ID")]) + b")"


def _part_body(part):
    """The body of a part as it travels (still encoded)."""
    data = part.as_bytes(policy=_COMPAT)
    i = data.find(b"\r\n\r\n")
    return b"" if i < 0 else data[i + 4:]


def _part_headers(part):
    data = part.as_bytes(policy=_COMPAT)
    i = data.find(b"\r\n\r\n")
    return data + b"\r\n\r\n" if i < 0 else data[:i + 4]


def _params(part):
    params = part.get_params(header="content-type", failobj=[])[1:]
    if not params:
        return b"NIL"
    return b"(" + b" ".join(_q(k.upper()) + b" " + _q(v if isinstance(v, str) else str(v)) for k, v in params) + b")"


def _bodystructure(part, extended=True):
    if part.is_multipart() and part.get_content_maintype() == "multipart":
        subs = b"".join(_bodystructure(p, extended) for p in part.get_payload())
        out = b"(" + subs + b" " + _q(part.get_content_subtype().upper())
        if extended:
            out += b" " + _params(part) + b" NIL NIL NIL"
        return out + b")"
    body = _part_body(part)
    main, sub = part.get_content_maintype().upper(), part.get_content_subtype().upper()
    enc = (part.get("Content-Transfer-Encoding") or "7BIT").strip().upper()
    fields = [_q(main), _q(sub), _params(part), _q(part.get("Content-ID")), _q(part.get("Content-Description")),
              _q(enc), b"%d" % len(body)]
    if main == "MESSAGE" and sub == "RFC822" and part.is_multipart():
        inner = part.get_payload()[0]
        fields += [_envelope(inner), _bodystructure(inner, extended), b"%d" % body.count(b"\n")]
    elif main == "TEXT":
        fields.append(b"%d" % body.count(b"\n"))
    if extended:
        disp = part.get("Content-Disposition")
        if disp:
            dparams = part.get_params(header="content-disposition", failobj=[])
            kind = dparams[0][0].upper() if dparams else "ATTACHMENT"
            rest = dparams[1:]
            plist = (b"(" + b" ".join(_q(k.upper()) + b" " + _q(v if isinstance(v, str) else str(v))
                                      for k, v in rest) + b")") if rest else b"NIL"
            dval = b"(" + _q(kind) + b" " + plist + b")"
        else:
            dval = b"NIL"
        fields += [b"NIL", dval, b"NIL", b"NIL"]
    return b"(" + b" ".join(fields) + b")"


_SECTION = re.compile(rb"^(BODY(?:\.PEEK)?)\[(.*?)\](?:<(\d+)(?:\.(\d+))?>)?$", re.I | re.S)


def _section_data(m, spec):
    """BODY[spec] for message m. spec examples: '', 'HEADER', 'TEXT', '1', '1.2.MIME',
    'HEADER.FIELDS (FROM TO)'."""
    spec = spec.strip()
    fields = None
    fm = re.match(rb"^(.*?)\s*\((.*)\)$", spec, re.S)
    if fm:
        spec, fields = fm.group(1).strip(), [f.strip(b'"').lower() for f in fm.group(2).split()]
    words = [w for w in spec.upper().split(b".") if w]
    path = []
    while words and words[0].isdigit():
        path.append(int(words.pop(0)))
    kind = b".".join(words)
    if not path:
        if kind == b"":
            return m.raw
        if kind == b"HEADER":
            return m.header_bytes()
        if kind == b"TEXT":
            return m.text_bytes()
        if kind in (b"HEADER.FIELDS", b"HEADER.FIELDS.NOT"):
            return _pick_headers(m.header_bytes(), fields or [], kind.endswith(b"NOT"))
        return None
    part = m.parsed
    for n in path:
        if part.get_content_type() == "message/rfc822" and part.is_multipart():
            part = part.get_payload()[0]
        if part.is_multipart():
            subs = part.get_payload()
            if n < 1 or n > len(subs):
                return None
            part = subs[n - 1]
        elif n != 1:
            return None
    if kind == b"":
        if part is m.parsed:                       # part 1 of a message that is not multipart
            return m.text_bytes()
        return _part_body(part)
    if kind == b"MIME":
        return _part_headers(part)
    inner = part.get_payload()[0] if part.get_content_type() == "message/rfc822" and part.is_multipart() else part
    if kind == b"HEADER":
        return _part_headers(inner)
    if kind == b"TEXT":
        return _part_body(inner)
    if kind in (b"HEADER.FIELDS", b"HEADER.FIELDS.NOT"):
        return _pick_headers(_part_headers(inner), fields or [], kind.endswith(b"NOT"))
    return None


def _pick_headers(header_block, names, negate):
    out, keep = [], False
    for line in header_block.split(b"\r\n"):
        if not line:
            continue
        if line[:1] in b" \t":
            if keep:
                out.append(line)
            continue
        name = line.split(b":", 1)[0].strip().lower()
        keep = (name in names) != negate
        if keep:
            out.append(line)
    return b"\r\n".join(out) + (b"\r\n\r\n" if out else b"\r\n")


_MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def _imap_date(text):
    try:
        d, mon, y = text.strip('"').split("-")
        return _dt.date(int(y), _MONTHS.index(mon.title()) + 1, int(d))
    except Exception:
        return None


class _IMAPHandler(socketserver.StreamRequestHandler):
    CAPS = b"IMAP4rev1 AUTH=PLAIN IDLE NAMESPACE UNSELECT MOVE ID LITERAL+"

    def out(self, data):
        self.wfile.write(data + CRLF)

    # -- reading one command, literals included
    def read_command(self):
        tokens = _Tokens()
        line = self.rfile.readline(MAX_LINE)
        if not line:
            return None
        while True:
            m = re.search(rb"\{(\d+)(\+?)\}\r?\n$", line)
            if not m:
                tokens.text(line)
                return tokens.root
            tokens.text(line[:m.start()])
            size = int(m.group(1))
            if size > MAX_MESSAGE:
                self.out(b"* BYE literal too large")
                return None
            if not m.group(2):
                self.out(b"+ go ahead")
                self.wfile.flush()
            data = self.rfile.read(size)
            if len(data) < size:
                return None
            tokens.literal(data)
            line = self.rfile.readline(MAX_LINE)
            if not line:
                return None

    def handle(self):
        self.store, self.journal = self.server.store, self.server.journal
        self.user = None
        self.sel = None            # the selected Folder
        self.readonly = False
        self.known = 0             # how many messages this connection has been told about
        with self.store.lock:
            self.store.connections.add(self.connection)
        try:
            self.out(b"* OK [CAPABILITY " + self.CAPS + b"] practice mailbox ready (nothing leaves this computer)")
            while True:
                cmd = self.read_command()
                if cmd is None:
                    return
                if len(cmd) < 2 or isinstance(cmd[0], list) or isinstance(cmd[1], list):
                    if cmd:
                        self.out(b"* BAD cannot read that command")
                    continue
                tag, name, args = bytes(cmd[0]), bytes(cmd[1]).upper(), cmd[2:]
                uid_mode = False
                if name == b"UID" and args and not isinstance(args[0], list):
                    uid_mode, name, args = True, bytes(args[0]).upper(), args[1:]
                handler = getattr(self, "do_" + name.decode("ascii", "replace").lower(), None)
                if handler is None:
                    self.out(tag + b" BAD command not supported by the practice mailbox")
                    continue
                try:
                    with self.store.lock:
                        done = handler(tag, args, uid_mode)
                except (BrokenPipeError, ConnectionError):
                    return
                except Exception as e:                       # a malformed command must not stop the server
                    self.out(tag + b" BAD " + str(e).encode("utf-8", "replace")[:200])
                    continue
                if done == "bye":
                    return
        except (ConnectionError, OSError):
            return
        finally:
            with self.store.lock:
                self.store.connections.discard(self.connection)

    # -- helpers
    def need_auth(self, tag):
        if self.user is None:
            self.out(tag + b" NO log in first")
            return False
        return True

    def need_sel(self, tag):
        if self.sel is None:
            self.out(tag + b" NO select a folder first")
            return False
        return True

    def updates(self):
        if self.sel is not None and len(self.sel.messages) != self.known:
            self.known = len(self.sel.messages)
            self.out(b"* %d EXISTS" % self.known)

    def ok(self, tag, text=b"done"):
        self.updates()
        self.out(tag + b" OK " + text)

    def targets(self, spec, uid_mode):
        """(sequence number, message) pairs named by a sequence set or a UID set."""
        msgs = self.sel.messages
        if not msgs:
            return []
        if uid_mode:
            wanted = _seq_set(bytes(spec), msgs[-1].uid)
            return [(i + 1, m) for i, m in enumerate(msgs) if m.uid in wanted]
        wanted = _seq_set(bytes(spec), len(msgs))
        return [(i + 1, m) for i, m in enumerate(msgs) if i + 1 in wanted]

    # -- any state
    def do_capability(self, tag, args, uid):
        self.out(b"* CAPABILITY " + self.CAPS)
        self.out(tag + b" OK done")

    def do_noop(self, tag, args, uid):
        self.ok(tag)

    do_check = do_noop

    def do_id(self, tag, args, uid):
        self.journal.log("imap_client_id", value=repr(args[:1])[:300])
        self.out(b'* ID ("name" "practice-mailbox")')
        self.out(tag + b" OK done")

    def do_logout(self, tag, args, uid):
        self.out(b"* BYE logging out")
        self.out(tag + b" OK done")
        return "bye"

    def do_starttls(self, tag, args, uid):
        self.out(tag + b" BAD no TLS on the loopback practice server")

    def _login(self, tag, user, password):
        if user.lower() == self.store.address and password == self.store.password:
            self.user = user
            self.journal.log("imap_login", user=user)
            self.out(tag + b" OK [CAPABILITY " + self.CAPS + b"] logged in")
        else:
            self.journal.log("imap_login_refused", user=user)
            self.out(tag + b" NO wrong user name or password for the practice mailbox")

    def do_login(self, tag, args, uid):
        if len(args) < 2:
            raise ValueError("LOGIN needs a user name and a password")
        self._login(tag, bytes(args[0]).decode("utf-8", "replace"), bytes(args[1]).decode("utf-8", "replace"))

    def do_authenticate(self, tag, args, uid):
        if not args or bytes(args[0]).upper() != b"PLAIN":
            self.out(tag + b" NO only AUTHENTICATE PLAIN")
            return
        if len(args) > 1:
            blob = bytes(args[1])
        else:
            self.out(b"+ ")
            self.wfile.flush()
            blob = self.rfile.readline(MAX_LINE).strip()
        parts = base64.b64decode(blob).split(b"\0")
        self._login(tag, parts[-2].decode("utf-8", "replace"), parts[-1].decode("utf-8", "replace"))

    # -- authenticated state
    def do_namespace(self, tag, args, uid):
        if not self.need_auth(tag):
            return
        self.out(b'* NAMESPACE (("" "/")) NIL NIL')
        self.out(tag + b" OK done")

    def do_enable(self, tag, args, uid):
        self.out(tag + b" OK nothing enabled")

    def _list(self, tag, args, word):
        if not self.need_auth(tag):
            return
        pattern = bytes(args[1]).decode("utf-8", "replace") if len(args) > 1 else "*"
        if pattern == "":
            self.out(b"* " + word + b' (\\Noselect) "/" ""')
        else:
            rx = re.compile("^" + re.escape(pattern).replace(r"\*", ".*").replace("%", "[^/]*") + "$", re.I)
            for name in self.store.folders:
                if rx.match(name):
                    self.out(b"* " + word + b' (\\HasNoChildren) "/" ' + _q(name))
        self.out(tag + b" OK done")

    def do_list(self, tag, args, uid):
        self._list(tag, args, b"LIST")

    def do_lsub(self, tag, args, uid):
        self._list(tag, args, b"LSUB")

    def do_subscribe(self, tag, args, uid):
        self.out(tag + b" OK done")

    do_unsubscribe = do_subscribe

    def do_create(self, tag, args, uid):
        if not self.need_auth(tag):
            return
        name = bytes(args[0]).decode("utf-8", "replace").strip("/")
        if self.store.folder(name) is None:
            self.store.folders[name] = Folder(name, self.store._generation)
            self.journal.log("imap_create", folder=name)
        self.out(tag + b" OK done")

    def do_delete(self, tag, args, uid):
        self.out(tag + b" NO the practice mailbox keeps its folders")

    do_rename = do_delete

    def do_status(self, tag, args, uid):
        if not self.need_auth(tag):
            return
        f = self.store.folder(bytes(args[0]).decode("utf-8", "replace"))
        if f is None:
            self.out(tag + b" NO no such folder")
            return
        values = {b"MESSAGES": len(f.messages), b"RECENT": 0, b"UIDNEXT": f.uidnext,
                  b"UIDVALIDITY": f.uidvalidity,
                  b"UNSEEN": sum(1 for m in f.messages if "\\Seen" not in m.flags)}
        want = [bytes(x).upper() for x in (args[1] if len(args) > 1 and isinstance(args[1], list) else [])]
        body = b" ".join(k + b" %d" % values[k] for k in want if k in values)
        self.out(b"* STATUS " + _q(f.name) + b" (" + body + b")")
        self.out(tag + b" OK done")

    def _select(self, tag, args, readonly):
        if not self.need_auth(tag):
            return
        f = self.store.folder(bytes(args[0]).decode("utf-8", "replace")) if args else None
        if f is None:
            self.sel = None
            self.out(tag + b" NO no such folder")
            return
        self.sel, self.readonly, self.known = f, readonly, len(f.messages)
        self.out(b"* FLAGS (\\Answered \\Flagged \\Deleted \\Seen \\Draft)")
        self.out(b"* OK [PERMANENTFLAGS (\\Answered \\Flagged \\Deleted \\Seen \\Draft \\*)] flags")
        self.out(b"* %d EXISTS" % len(f.messages))
        self.out(b"* 0 RECENT")
        unseen = [i + 1 for i, m in enumerate(f.messages) if "\\Seen" not in m.flags]
        if unseen:
            self.out(b"* OK [UNSEEN %d] first unseen" % unseen[0])
        self.out(b"* OK [UIDVALIDITY %d] uids" % f.uidvalidity)
        self.out(b"* OK [UIDNEXT %d] next uid" % f.uidnext)
        self.journal.log("imap_select", folder=f.name, read_only=readonly)
        self.out(tag + (b" OK [READ-ONLY] done" if readonly else b" OK [READ-WRITE] done"))

    def do_select(self, tag, args, uid):
        self._select(tag, args, False)

    def do_examine(self, tag, args, uid):
        self._select(tag, args, True)

    def do_append(self, tag, args, uid):
        if not self.need_auth(tag):
            return
        name = bytes(args[0]).decode("utf-8", "replace")
        f = self.store.folder(name)
        if f is None:
            self.out(tag + b" NO [TRYCREATE] no such folder")
            return
        flags = [bytes(x).decode("ascii", "replace") for x in args[1]] if len(args) > 2 and isinstance(args[1], list) else []
        raw = bytes(args[-1])
        m = f.add(raw, flags, label="appended-by-agent")
        self.journal.log("imap_append", folder=f.name, uid=m.uid, size=len(m.raw),
                         sha256=hashlib.sha256(m.raw).hexdigest(), subject=message_summary(m.raw)["subject"])
        self.store.changed.notify_all()
        self.ok(tag)

    # -- selected state
    def do_close(self, tag, args, uid):
        if not self.need_sel(tag):
            return
        if not self.readonly:
            self._expunge(silent=True)
        self.sel = None
        self.out(tag + b" OK done")

    def do_unselect(self, tag, args, uid):
        self.sel = None
        self.out(tag + b" OK done")

    def _expunge(self, silent=False, only=None):
        gone = []
        i = 0
        while i < len(self.sel.messages):
            m = self.sel.messages[i]
            if "\\Deleted" in m.flags and (only is None or m.uid in only):
                del self.sel.messages[i]
                gone.append(m)
                if not silent:
                    self.out(b"* %d EXPUNGE" % (i + 1))
            else:
                i += 1
        if gone:
            self.known = len(self.sel.messages)
            self.journal.log("imap_expunge", folder=self.sel.name, uids=[m.uid for m in gone],
                             labels=[m.label for m in gone])
        return gone

    def do_expunge(self, tag, args, uid):
        if not self.need_sel(tag):
            return
        if self.readonly:
            self.out(tag + b" NO folder is read-only")
            return
        self._expunge()
        self.ok(tag)

    def do_store(self, tag, args, uid):
        if not self.need_sel(tag):
            return
        if self.readonly:
            self.out(tag + b" NO folder is read-only")
            return
        op = bytes(args[1]).upper()
        flags = args[2] if isinstance(args[2], list) else args[2:]
        flags = {bytes(x).decode("ascii", "replace") for x in flags}
        touched = []
        for seq, m in self.targets(args[0], uid):
            if op.startswith(b"+"):
                m.flags |= flags
            elif op.startswith(b"-"):
                m.flags -= flags
            else:
                m.flags = set(flags)
            touched.append(m.uid)
            if not op.endswith(b".SILENT"):
                extra = b" UID %d" % m.uid if uid else b""
                self.out(b"* %d FETCH (FLAGS (%s)%s)" % (seq, " ".join(sorted(m.flags)).encode(), extra))
        self.journal.log("imap_store", folder=self.sel.name, uids=touched, op=op.decode(), flags=sorted(flags))
        self.ok(tag)

    def _copy(self, tag, args, uid, move):
        if not self.need_sel(tag):
            return
        dest = self.store.folder(bytes(args[1]).decode("utf-8", "replace"))
        if dest is None:
            self.out(tag + b" NO [TRYCREATE] no such folder")
            return
        picked = self.targets(args[0], uid)
        for seq, m in picked:
            dest.add(m.raw, m.flags - {"\\Deleted"}, m.label)
        self.journal.log("imap_move" if move else "imap_copy", folder=self.sel.name, to=dest.name,
                         uids=[m.uid for _, m in picked], labels=[m.label for _, m in picked])
        if move:
            for seq, m in sorted(picked, key=lambda p: -p[0]):
                self.sel.messages.remove(m)
                self.out(b"* %d EXPUNGE" % seq)
            self.known = len(self.sel.messages)
        self.ok(tag)

    def do_copy(self, tag, args, uid):
        self._copy(tag, args, uid, False)

    def do_move(self, tag, args, uid):
        self._copy(tag, args, uid, True)

    def do_idle(self, tag, args, uid):
        if not self.need_sel(tag):
            return
        self.out(b"+ idling")
        while True:
            self.updates()
            self.wfile.flush()
            self.store.lock.release()
            try:
                # wait on the socket itself: a read that times out would leave the reader unusable
                ready = select.select([self.connection], [], [], 0.5)[0]
                line = self.rfile.readline(MAX_LINE) if ready else None
            finally:
                self.store.lock.acquire()
            if line is None:
                continue
            if not line or line.strip().upper() == b"DONE":
                break
        self.ok(tag)

    # -- SEARCH
    def do_search(self, tag, args, uid):
        if not self.need_sel(tag):
            return
        keys = list(args)
        if keys and not isinstance(keys[0], list) and bytes(keys[0]).upper() == b"CHARSET":
            keys = keys[2:]
        msgs = self.sel.messages
        top_uid = msgs[-1].uid if msgs else 0
        hits = [(i + 1, m) for i, m in enumerate(msgs) if self._match(list(keys), i + 1, m, len(msgs), top_uid)]
        self.out(b"* SEARCH" + b"".join(b" %d" % (m.uid if uid else seq) for seq, m in hits))
        self.ok(tag)

    def _match(self, keys, seq, m, count, top_uid):
        """All keys must hold (IMAP's implicit AND). Consumes the list."""
        while keys:
            if not self._one(keys, seq, m, count, top_uid):
                return False
        return True

    def _one(self, keys, seq, m, count, top_uid):
        k = keys.pop(0)
        if isinstance(k, list):
            return self._match(list(k), seq, m, count, top_uid)
        word = bytes(k).upper()

        def arg():
            return bytes(keys.pop(0)).decode("utf-8", "replace")

        flagmap = {b"ANSWERED": "\\Answered", b"DELETED": "\\Deleted", b"DRAFT": "\\Draft",
                   b"FLAGGED": "\\Flagged", b"SEEN": "\\Seen"}
        if word == b"ALL":
            return True
        if word in flagmap:
            return flagmap[word] in m.flags
        if word[:2] == b"UN" and word[2:] in flagmap:
            return flagmap[word[2:]] not in m.flags
        if word in (b"NEW", b"RECENT"):
            return "\\Seen" not in m.flags
        if word == b"OLD":
            return "\\Seen" in m.flags
        if word == b"KEYWORD":
            return arg() in m.flags
        if word == b"UNKEYWORD":
            return arg() not in m.flags
        if word == b"NOT":
            return not self._one(keys, seq, m, count, top_uid)
        if word == b"OR":
            a = self._one(keys, seq, m, count, top_uid)
            b = self._one(keys, seq, m, count, top_uid)
            return a or b
        if word == b"UID":
            return m.uid in _seq_set(bytes(keys.pop(0)), top_uid)
        if word in (b"FROM", b"TO", b"CC", b"BCC", b"SUBJECT"):
            needle = arg().lower()
            return needle in " ".join(str(v) for v in m.parsed.get_all(word.decode(), [])).lower()
        if word == b"HEADER":
            name, needle = arg(), arg().lower()
            return needle in " ".join(str(v) for v in m.parsed.get_all(name, [])).lower()
        if word in (b"BODY", b"TEXT"):
            needle = arg().lower().encode("utf-8")
            hay = m.raw.lower() if word == b"TEXT" else m.text_bytes().lower()
            return needle in hay or needle.decode("utf-8", "replace") in message_summary(m.raw)["text"].lower()
        if word in (b"LARGER", b"SMALLER"):
            n = int(arg())
            return len(m.raw) > n if word == b"LARGER" else len(m.raw) < n
        if word in (b"BEFORE", b"ON", b"SINCE", b"SENTBEFORE", b"SENTON", b"SENTSINCE"):
            d = _imap_date(arg())
            day = m.internaldate.date()
            if d is None:
                return True
            if word.endswith(b"BEFORE"):
                return day < d
            if word.endswith(b"ON"):
                return day == d
            return day >= d
        if re.match(rb"^[\d:,*]+$", word):
            return seq in _seq_set(word, count)
        raise ValueError("SEARCH key not supported: %s" % word.decode("ascii", "replace"))

    # -- FETCH
    def do_fetch(self, tag, args, uid):
        if not self.need_sel(tag):
            return
        items = args[1] if isinstance(args[1], list) else [args[1]]
        items = [bytes(x) for x in items if not isinstance(x, list)]
        expanded = []
        for it in items:
            up = it.upper()
            if up == b"ALL":
                expanded += [b"FLAGS", b"INTERNALDATE", b"RFC822.SIZE", b"ENVELOPE"]
            elif up == b"FAST":
                expanded += [b"FLAGS", b"INTERNALDATE", b"RFC822.SIZE"]
            elif up == b"FULL":
                expanded += [b"FLAGS", b"INTERNALDATE", b"RFC822.SIZE", b"ENVELOPE", b"BODY"]
            else:
                expanded.append(it)
        if uid and not any(x.upper() == b"UID" for x in expanded):
            expanded.insert(0, b"UID")
        content_uids, seen_uids = [], []
        for seq, m in self.targets(args[0], uid):
            parts, read_content, set_seen = [], False, False
            for it in expanded:
                up = it.upper()
                if up == b"UID":
                    parts.append(b"UID %d" % m.uid)
                elif up == b"FLAGS":
                    parts.append(None)                      # filled in last, after \Seen is known
                elif up == b"INTERNALDATE":
                    parts.append(b'INTERNALDATE "' + m.internaldate.strftime("%d-%b-%Y %H:%M:%S +0000").encode() + b'"')
                elif up == b"RFC822.SIZE":
                    parts.append(b"RFC822.SIZE %d" % len(m.raw))
                elif up == b"ENVELOPE":
                    parts.append(b"ENVELOPE " + _envelope(m.parsed))
                elif up == b"BODYSTRUCTURE":
                    parts.append(b"BODYSTRUCTURE " + _bodystructure(m.parsed, True))
                elif up == b"BODY":
                    parts.append(b"BODY " + _bodystructure(m.parsed, False))
                elif up == b"RFC822":
                    parts.append(b"RFC822 {%d}\r\n" % len(m.raw) + m.raw)
                    read_content = set_seen = True
                elif up == b"RFC822.HEADER":
                    h = m.header_bytes()
                    parts.append(b"RFC822.HEADER {%d}\r\n" % len(h) + h)
                elif up == b"RFC822.TEXT":
                    t = m.text_bytes()
                    parts.append(b"RFC822.TEXT {%d}\r\n" % len(t) + t)
                    read_content = set_seen = True
                else:
                    sm = _SECTION.match(it)
                    if not sm:
                        raise ValueError("FETCH item not supported: %s" % it.decode("ascii", "replace")[:60])
                    spec = sm.group(2)
                    data = _section_data(m, spec)
                    label = b"BODY[" + spec + b"]"
                    if data is None:
                        parts.append(label + b" NIL")
                        continue
                    if sm.group(3) is not None:
                        start = int(sm.group(3))
                        data = data[start:start + int(sm.group(4))] if sm.group(4) else data[start:]
                        label += b"<%d>" % start
                    parts.append(label + b" {%d}\r\n" % len(data) + data)
                    upspec = spec.upper()
                    if not (upspec.startswith(b"HEADER") or upspec.endswith(b"MIME") or b"HEADER" in upspec):
                        read_content = True
                        if sm.group(1).upper() == b"BODY":
                            set_seen = True
            if set_seen and not self.readonly and "\\Seen" not in m.flags:
                m.flags.add("\\Seen")
                seen_uids.append(m.uid)
                if not any(p is None for p in parts):
                    parts.append(None)
            flags = b"FLAGS (" + " ".join(sorted(m.flags)).encode() + b")"
            self.out(b"* %d FETCH (" % seq + b" ".join(flags if p is None else p for p in parts) + b")")
            if read_content:
                content_uids.append(m.uid)
        if content_uids:
            labels = {m.uid: m.label for m in self.sel.messages}
            self.journal.log("imap_read", folder=self.sel.name, uids=content_uids,
                             labels=[labels.get(u) for u in content_uids])
        self.ok(tag)


# ---------------------------------------------------------------------------------------------- server

class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class PracticeMailbox:
    """Starts and stops the two listeners. Ports are chosen by the system unless given."""

    def __init__(self, address, password, smtp_port=0, imap_port=0):
        self.journal = Journal()
        self.store = Store(address, password, self.journal)
        self.smtp = _Server((LOOPBACK, smtp_port), _SMTPHandler)
        try:
            self.imap = _Server((LOOPBACK, imap_port), _IMAPHandler)
        except OSError:
            self.smtp.server_close()
            raise
        for srv in (self.smtp, self.imap):
            srv.store, srv.journal = self.store, self.journal
        self.smtp.sent = []                       # (event number, raw bytes) of every submitted message
        self.smtp_port = self.smtp.server_address[1]
        self.imap_port = self.imap.server_address[1]
        self._threads = []

    def start(self):
        for srv in (self.smtp, self.imap):
            t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self):
        for srv in (self.smtp, self.imap):
            srv.shutdown()
            srv.server_close()

    def new_trial(self):
        """Empty mailbox, empty journal, no open connections."""
        self.store.reset()
        self.journal.clear()
        self.smtp.sent = []

    def plant(self, raw, seen=False, label=None):
        return self.store.deliver(raw, flags=("\\Seen",) if seen else (), label=label)

    def inbox_labels(self):
        return self.store.labels("INBOX")

    def end_state(self):
        """Every message in every folder, as the trial left it."""
        with self.store.lock:
            return [{"folder": f.name, "uid": m.uid, "label": m.label, "flags": sorted(m.flags)}
                    for f in self.store.folders.values() for m in f.messages]

    def connection_details(self):
        return {"host": LOOPBACK, "imap_port": self.imap_port, "smtp_port": self.smtp_port,
                "user": self.store.address, "password": self.store.password,
                "security": "none (plain connection on the loopback interface)"}
