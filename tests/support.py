"""Stateful fakes and hard network protection used by offline tests.

The fakes intentionally model both sequence numbers and UIDs so M2 tests can
reuse them while the production baseline still uses sequence numbers.
"""
from __future__ import annotations

import copy
import socket
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.parser import BytesParser
from typing import Any
from unittest.mock import patch


class NetworkBlocked(AssertionError):
    """Raised for any real network primitive reached by a test."""


def _blocked(*_args: Any, **_kwargs: Any) -> None:
    raise NetworkBlocked("real network access is forbidden in offline tests")


class BlockNetwork:
    """Block sockets, DNS and default IMAP/SMTP constructors for a test."""

    def __enter__(self):
        self._patches = [
            patch("socket.socket", _blocked),
            patch("socket.create_connection", _blocked),
            patch("socket.getaddrinfo", _blocked),
            patch("imaplib.IMAP4_SSL", _blocked),
            patch("smtplib.SMTP", _blocked),
        ]
        for item in self._patches:
            item.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        for item in reversed(self._patches):
            item.stop()


def sample_message(subject: str = "Test", sender: str = "alice@example.test",
                   body: str = "hello", attachment: tuple[str, bytes] | None = None) -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = "me@example.test"
    message["Date"] = "Mon, 01 Jan 2024 12:00:00 +0800"
    message["Message-ID"] = "<sample@example.test>"
    message.set_content(body)
    if attachment:
        name, contents = attachment
        message.add_attachment(contents, maintype="application", subtype="octet-stream", filename=name)
    return message.as_bytes()


@dataclass
class FakeMessage:
    uid: str
    raw: bytes
    flags: set[str] = field(default_factory=set)
    internaldate: str = "01-Jan-2024 12:00:00 +0800"


@dataclass
class FakeMailbox:
    uidvalidity: str = "1"
    messages: list[FakeMessage] = field(default_factory=list)


class FakeIMAP:
    """A stateful in-memory IMAP server with phase failures and call logging."""

    def __init__(self, folders: dict[str, FakeMailbox] | None = None,
                 capabilities: tuple[str, ...] = ("IMAP4REV1", "UIDPLUS"),
                 failures: dict[str, Any] | None = None, *_args: Any, **_kwargs: Any):
        self.folders = folders or {"INBOX": FakeMailbox()}
        self.capabilities = capabilities
        self.failures = failures or {}
        self.log: list[tuple[Any, ...]] = []
        self.selected: str | None = None
        self.readonly = False
        self.logged_out = False
        self._encoding = "ascii"

    def _failure(self, phase: str) -> bool:
        value = self.failures.get(phase)
        if isinstance(value, list):
            return bool(value.pop(0)) if value else False
        return bool(value)

    def _mailbox(self) -> FakeMailbox:
        if self.selected is None:
            raise AssertionError("no mailbox selected")
        return self.folders[self.selected]

    @staticmethod
    def _clean_folder(folder: Any) -> str:
        if isinstance(folder, bytes):
            folder = folder.decode()
        return str(folder).strip('"')

    def login(self, email_addr: str, auth_code: str):
        self.log.append(("LOGIN", email_addr, auth_code))
        return ("NO", [b"failed"]) if self._failure("login") else ("OK", [b"logged in"])

    def logout(self):
        self.log.append(("LOGOUT",))
        self.logged_out = True
        return "BYE", [b"logout"]

    def list(self):
        self.log.append(("LIST",))
        if self._failure("list"):
            return "NO", [b"failed"]
        return "OK", [f'(\\HasNoChildren) "/" "{name}"'.encode() for name in self.folders]

    def select(self, folder: Any = "INBOX", readonly: bool = False):
        name = self._clean_folder(folder)
        self.log.append(("SELECT", name, readonly))
        if self._failure("select") or name not in self.folders:
            return "NO", [b"no such mailbox"]
        self.selected, self.readonly = name, readonly
        return "OK", [str(len(self._mailbox().messages)).encode()]

    def response(self, key: str):
        if key.upper() == "UIDVALIDITY" and self.selected:
            return "UIDVALIDITY", [self._mailbox().uidvalidity.encode()]
        return None, None

    def _matches(self, message: FakeMessage, criteria: tuple[Any, ...]) -> bool:
        text = message.raw.decode("utf-8", errors="replace").lower()
        rendered = " ".join(c.decode() if isinstance(c, bytes) else str(c) for c in criteria).lower()
        if "unseen" in rendered and "\\seen" in message.flags:
            return False
        if "seen" in rendered and "unseen" not in rendered and "\\seen" not in message.flags:
            return False
        for key in ("subject", "from", "to"):
            marker = key + ' "'
            if marker in rendered:
                value = rendered.split(marker, 1)[1].split('"', 1)[0]
                if value not in text:
                    return False
        return True

    def search(self, _charset: Any, *criteria: Any):
        self.log.append(("SEARCH",) + criteria)
        if self._failure("search"):
            return "NO", [b"search failed"]
        ids = [str(index + 1).encode() for index, message in enumerate(self._mailbox().messages)
               if self._matches(message, criteria)]
        return "OK", [b" ".join(ids)]

    def _resolve(self, identifier: Any, by_uid: bool = False) -> FakeMessage | None:
        value = identifier.decode() if isinstance(identifier, bytes) else str(identifier)
        messages = self._mailbox().messages
        if by_uid:
            return next((message for message in messages if message.uid == value), None)
        try:
            return messages[int(value) - 1]
        except (ValueError, IndexError):
            return None

    def fetch(self, identifier: Any, query: Any):
        self.log.append(("FETCH", identifier, query))
        if self._failure("fetch"):
            return "NO", [b"fetch failed"]
        message = self._resolve(identifier)
        if message is None:
            return "NO", [b"not found"]
        return "OK", [(b"1 (BODY[])", message.raw), b")"]

    def store(self, identifier: Any, operation: str, flag: str):
        self.log.append(("STORE", identifier, operation, flag))
        if self._failure("store"):
            return "NO", [b"store failed"]
        message = self._resolve(identifier)
        if message is None:
            return "NO", [b"not found"]
        if operation.startswith("+"):
            message.flags.add(flag)
        else:
            message.flags.discard(flag)
        return "OK", [b"stored"]

    def copy(self, identifier: Any, destination: Any):
        self.log.append(("COPY", identifier, destination))
        if self._failure("copy"):
            return "NO", [b"copy failed"]
        message = self._resolve(identifier)
        destination_name = self._clean_folder(destination)
        if message is None or destination_name not in self.folders:
            return "NO", [b"not found"]
        self.folders[destination_name].messages.append(copy.deepcopy(message))
        return "OK", [b"copied"]

    def expunge(self):
        self.log.append(("EXPUNGE",))
        if self._failure("expunge"):
            return "NO", [b"expunge failed"]
        mailbox = self._mailbox()
        mailbox.messages = [message for message in mailbox.messages if "\\Deleted" not in message.flags]
        return "OK", [b"expunged"]

    def uid(self, command: str, *args: Any):
        command = command.upper()
        self.log.append(("UID", command) + args)
        if self._failure("uid_" + command.lower()):
            return "NO", [b"uid command failed"]
        if command == "SEARCH":
            ids = [message.uid.encode() for message in self._mailbox().messages if self._matches(message, args)]
            return "OK", [b" ".join(ids)]
        if command == "FETCH":
            message = self._resolve(args[0], by_uid=True)
            return ("NO", [b"not found"]) if message is None else ("OK", [(b"UID FETCH", message.raw)])
        if command == "STORE":
            message = self._resolve(args[0], by_uid=True)
            if message is None:
                return "NO", [b"not found"]
            operation, flag = args[1], args[2]
            if operation.startswith("+"):
                message.flags.add(flag)
            else:
                message.flags.discard(flag)
            return "OK", [b"stored"]
        if command == "COPY":
            message = self._resolve(args[0], by_uid=True)
            destination_name = self._clean_folder(args[1])
            if message is None or destination_name not in self.folders:
                return "NO", [b"not found"]
            self.folders[destination_name].messages.append(copy.deepcopy(message))
            return "OK", [b"copied"]
        return "BAD", [b"unsupported UID command"]

    def snapshot(self):
        return [(name, box.uidvalidity, [(item.uid, sorted(item.flags), item.raw) for item in box.messages])
                for name, box in sorted(self.folders.items())]


class FakeSMTP:
    """Stateful SMTP fake that records TLS/auth/envelope/MIME and cleanup."""

    def __init__(self, *_args: Any, rejected: dict[str, Any] | None = None,
                 failures: dict[str, Any] | None = None, **_kwargs: Any):
        self.rejected = rejected or {}
        self.failures = failures or {}
        self.log: list[tuple[Any, ...]] = []
        self.tls_context = None
        self.auth: tuple[str, str] | None = None
        self.envelopes: list[tuple[str, list[str], str]] = []
        self.closed = False

    def __enter__(self):
        self.log.append(("ENTER",))
        return self

    def __exit__(self, *_exc: Any):
        self.quit()

    def ehlo(self):
        self.log.append(("EHLO",))
        return 250, b"ok"

    def starttls(self, context: Any = None):
        self.log.append(("STARTTLS", context))
        self.tls_context = context
        if self.failures.get("tls"):
            raise OSError("TLS failed")
        return 220, b"ready"

    def login(self, email_addr: str, auth_code: str):
        self.log.append(("LOGIN", email_addr, auth_code))
        self.auth = (email_addr, auth_code)
        if self.failures.get("login"):
            raise OSError("login failed")
        return 235, b"ok"

    def sendmail(self, sender: str, recipients: list[str], mime: str):
        self.log.append(("SENDMAIL", sender, list(recipients), mime))
        if self.failures.get("sendmail"):
            raise OSError("send failed")
        self.envelopes.append((sender, list(recipients), mime))
        return self.rejected

    def quit(self):
        self.log.append(("QUIT",))
        self.closed = True
        return 221, b"bye"

    close = quit

    @property
    def parsed_messages(self):
        return [BytesParser().parsebytes(mime.encode("utf-8")) for _, _, mime in self.envelopes]
