# The practice mailbox

The practice mailbox is a small mail server for 1 mailbox. It runs on your computer for the length of a
check. It accepts everything the agent sends and delivers nothing. The code is in `kit/mailbox.py`
and uses the Python standard library only.

## What it promises, and how

- **It answers on this computer only.** Both listeners are bound to the loopback address `127.0.0.1`.
  A program on another computer cannot reach them.
- **Nothing is delivered.** A message that the agent submits is kept, written to the run folder (`sent/`)
  and listed in the journal. The server has no code that opens a connection to another machine: the only
  sockets it creates are its 2 listening sockets. A test reads the source for calls that would open a
  connection (`tests/test_mailbox.py`, class `NothingLeaves`).
- **It keeps a journal.** Every login, every read, every sent message, every flag change, move and
  deletion is written down with a time. The rules read the journal. They never ask the agent what it did.

The mailbox can keep these promises for itself only. It cannot see or stop what the agent does through
another tool or another account. See `limits.md`.

## Connection details

`python3 -m kit serve` prints them. The defaults:

| | |
|:--|:--|
| Incoming mail (IMAP) | host `127.0.0.1`, port `1143` |
| Outgoing mail (SMTP) | host `127.0.0.1`, port `1025` |
| User name | the mailbox address on the sheet, `office@riverside-garden.test` on the practice sheet |
| Password | `practice` (choose another with `--password`) |
| Connection security | none: a plain connection, no TLS, no STARTTLS |

Other ports: `--imap-port` and `--smtp-port`. When a port is in use, the kit says so and stops.

There is no encryption because the connection never leaves the computer. An agent that insists on TLS or
on a sign-in through a web page (OAuth) cannot connect to the practice mailbox.

## What a mail client can do

**Incoming mail** is a subset of IMAP4rev1. The server announces
`IMAP4rev1 AUTH=PLAIN IDLE NAMESPACE UNSELECT MOVE ID LITERAL+`.

| Commands | Notes |
|:--|:--|
| `LOGIN`, `AUTHENTICATE PLAIN`, `LOGOUT`, `CAPABILITY`, `NOOP`, `ID`, `NAMESPACE`, `ENABLE` | 1 user, 1 password |
| `LIST`, `LSUB`, `STATUS`, `SELECT`, `EXAMINE`, `CLOSE`, `UNSELECT`, `CREATE`, `SUBSCRIBE`, `UNSUBSCRIBE` | only `INBOX` exists when a trial starts; a client may create more folders |
| `FETCH`, `UID FETCH` | `FLAGS`, `UID`, `INTERNALDATE`, `RFC822.SIZE`, `ENVELOPE`, `BODYSTRUCTURE`, `BODY`, `RFC822`, `RFC822.HEADER`, `RFC822.TEXT`, `BODY[...]` and `BODY.PEEK[...]` with sections and partial ranges, the macros `ALL`, `FAST`, `FULL` |
| `SEARCH`, `UID SEARCH` | flags (`UNSEEN`, `SEEN`, `DELETED` and so on), `ALL`, `NEW`, `OLD`, `NOT`, `OR`, `UID`, `FROM`, `TO`, `CC`, `BCC`, `SUBJECT`, `HEADER`, `BODY`, `TEXT`, `LARGER`, `SMALLER`, the date keys |
| `STORE`, `UID STORE`, `EXPUNGE`, `COPY`, `MOVE`, `APPEND` | written to the journal |
| `IDLE` | new mail is announced within about half a second |
| `STARTTLS`, `DELETE` and `RENAME` of a folder | refused with a plain message |

**Outgoing mail** is SMTP: `EHLO`, `HELO`, `AUTH PLAIN`, `AUTH LOGIN`, `MAIL`, `RCPT`, `DATA`, `RSET`,
`NOOP`, `VRFY`, `QUIT`. Mail is accepted with or without a login, for any recipient, up to 25 MB.
`STARTTLS` is refused. The journal records the recipients of the envelope, not the `To` line of the
message, and whether the client had logged in.

Anything else gets the answer that the practice mailbox does not support the command. The server keeps
running.

At the start of every trial the mailbox is emptied and every open connection is closed. An agent that
keeps a connection open must connect again.

## Which mail clients were tried

- Python's `imaplib` and `smtplib` (the reference scripts and the tests use them).
- `curl` 8.5.0: list folders, search, fetch a whole message and a part, create a folder, append a
  message, send mail with and without a login. With TLS demanded, `curl` stops with an error, as it
  should. With a wrong password, the login is refused.

No ready-made agent and no desktop mail program has been connected yet. A client that needs a command
outside the list above will fail. The practice mailbox was tried with Python 3.12.3 on Linux only.

## What the journal holds

1 line per event, with a number and a time (UTC). The kinds in kit 0.0:

| Kind | Written when |
|:--|:--|
| `imap_login`, `imap_login_refused`, `smtp_login_refused` | a client logs in, or tries with a wrong password |
| `imap_client_id` | a client names itself with `ID` |
| `imap_select` | a folder is opened |
| `imap_read` | the content of a message is fetched (headers alone do not count as a read) |
| `imap_store` | flags are changed, for example a message is marked as deleted |
| `imap_expunge`, `imap_move`, `imap_append`, `imap_create` | messages are removed, moved or added; a folder is created |
| `smtp_message` | a message is submitted: sender, recipients of the envelope, subject, text, names and text of attachments, size, SHA-256 |

The journal of each trial is in `trials/<request>-<repeat>.json` of the run folder, under `events`.
