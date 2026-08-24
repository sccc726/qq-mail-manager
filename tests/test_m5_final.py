"""M5 contract and ownership gates (all fully offline)."""
from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import unittest
from unittest.mock import patch

from tests.test_m0_contract import ENTRYPOINTS, ROOT, SCRIPTS, load_script
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from qqmail_core.cli import parse_mailref_csv
from qqmail_core.mailref import MailRefError
from qqmail_core.mime import decode_header_value
from tests.support import BlockNetwork
from tests.support import FakeIMAP, FakeMailbox, FakeMessage, sample_message


class M5CliContractTests(unittest.TestCase):
    def test_shared_mailref_csv_rejects_invalid_values_without_credentials(self):
        self.assertEqual([item.uid for item in parse_mailref_csv("INBOX", "7", " 2,3 ")], ["2", "3"])
        with self.assertRaises(MailRefError):
            parse_mailref_csv("INBOX", "7", "1:*")

    def test_all_cli_static_errors_are_single_json_before_credentials_or_network(self):
        invocations = {
            "list_folders.py": ["--unknown"],
            "search_emails.py": ["--folder", "bad\nfolder"],
            "get_email.py": ["--mail_ids", "1:*", "--folder", "INBOX", "--uidvalidity", "1"],
            "download_attachment.py": ["--mail_ids", "1:*", "--folder", "INBOX", "--uidvalidity", "1"],
            "mark_email.py": ["--mail_ids", "1:*", "--action", "read", "--folder", "INBOX", "--uidvalidity", "1"],
            "move_email.py": ["--mail_ids", "1:*", "--src_folder", "INBOX", "--uidvalidity", "1", "--dst_folder", "Archive"],
            "send_email.py": ["--to", "invalid", "--subject", "s", "--body", "b"],
        }
        for name, arguments in invocations.items():
            module, stdout, stderr = load_script(name), io.StringIO(), io.StringIO()
            with BlockNetwork(), patch.dict(os.environ, {}, clear=True), \
                    patch.object(sys, "argv", [name, *arguments]), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = module.main()
            self.assertNotEqual(code, 0, name)
            self.assertEqual(stderr.getvalue(), "", name)
            self.assertEqual(len(stdout.getvalue().splitlines()), 1, name)
            self.assertEqual(json.loads(stdout.getvalue())["status"], "error", name)

    def test_all_cli_missing_credentials_are_rc1_single_json_under_network_block(self):
        invocations = {
            "list_folders.py": [], "search_emails.py": [],
            "get_email.py": ["--mail_ids", "1", "--folder", "INBOX", "--uidvalidity", "1"],
            "download_attachment.py": ["--mail_ids", "1", "--folder", "INBOX", "--uidvalidity", "1"],
            "mark_email.py": ["--mail_ids", "1", "--action", "read", "--folder", "INBOX", "--uidvalidity", "1"],
            "move_email.py": ["--mail_ids", "1", "--src_folder", "INBOX", "--uidvalidity", "1", "--dst_folder", "Archive"],
            "send_email.py": ["--to", "to@example.test", "--subject", "s", "--body", "b"],
        }
        for name, arguments in invocations.items():
            module, stdout, stderr = load_script(name), io.StringIO(), io.StringIO()
            with BlockNetwork(), patch.dict(os.environ, {}, clear=True), \
                    patch.object(sys, "argv", [name, *arguments]), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = module.main()
            self.assertEqual(code, 1, name)
            self.assertEqual(stderr.getvalue(), "", name)
            self.assertEqual(len(stdout.getvalue().splitlines()), 1, name)
            document = json.loads(stdout.getvalue())
            self.assertEqual((document["status"], document["code"]), ("error", "missing_credentials"), name)
    def test_entrypoints_preserve_public_helpers_after_migration(self):
        # These names are used by the long-lived feature suites.  Keeping this
        # explicit makes the thin-CLI migration a behavior-preserving move.
        expected = {
            "list_folders.py": ("list_folders",),
            "search_emails.py": ("query_emails", "build_search_criteria"),
            "get_email.py": ("get_emails", "fetch_single_email"),
            "download_attachment.py": ("download_attachments", "safe_filename"),
            "mark_email.py": ("mark_emails",),
            "move_email.py": ("move_emails", "preview_emails"),
            "send_email.py": ("send_email", "build_draft", "transmit_draft"),
        }
        for name, public_names in expected.items():
            module = load_script(name)
            for public_name in public_names:
                self.assertTrue(callable(getattr(module, public_name, None)), (name, public_name))

    def test_unknown_header_charset_preserves_move_and_reply_failures(self):
        encoded = "=?x-unknown?B?SGVsbG8=?="
        self.assertEqual(decode_header_value(encoded), "Hello")
        with self.assertRaises(LookupError):
            decode_header_value(encoded, strict_charset=True)

        raw = sample_message().replace(b"Subject: Test", b"Subject: " + encoded.encode())
        move = load_script("move_email.py")
        imap = FakeIMAP({"INBOX": FakeMailbox(messages=[FakeMessage("9", raw)]), "Archive": FakeMailbox()})
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=imap):
            moved = move.move_emails("me@example.test", "token", ["9"], "INBOX", "Archive", uidvalidity="1")
        self.assertEqual(moved["status"], "error")
        self.assertFalse(any(call[1] in {"MOVE", "COPY", "STORE", "EXPUNGE"}
                             for call in imap.log if call[:1] == ("UID",)))

        send = load_script("send_email.py")
        stdout, stderr = io.StringIO(), io.StringIO()
        reply_imap = FakeIMAP({"INBOX": FakeMailbox(messages=[FakeMessage("9", raw)])})
        with patch.dict(os.environ, {"QQ_EMAIL": "me@example.test", "QQ_EMAIL_AUTH_CODE": "token"}, clear=True), \
                patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=reply_imap), \
                patch.object(sys, "argv", ["send_email.py", "--reply-to-id", "9", "--reply-folder", "INBOX",
                                             "--reply-uidvalidity", "1", "--body", "reply"]), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = send.main()
        self.assertEqual((code, stderr.getvalue()), (1, ""))
        self.assertEqual(json.loads(stdout.getvalue())["code"], "send_build_failed")


class M5IsolationTests(unittest.TestCase):
    def test_each_milestone_module_runs_in_a_fresh_interpreter(self):
        if os.environ.get("QQMAIL_M5_ISOLATED_CHILD") == "1":
            return
        modules = ("test_m0_contract", "test_m1_core", "test_m2_uid_migration",
                   "test_m3_send", "test_m4_hardening", "test_m5_final")
        # Start from the platform environment so Windows TEMP/TMP/SystemRoot
        # and PATH remain usable.  Remove Python import injection rather than
        # relying on a pre-set scripts path, then explicitly neutralize mail
        # credentials for every fresh interpreter.
        environment = os.environ.copy()
        for key in tuple(environment):
            if key.startswith("PYTHON"):
                environment.pop(key)
        environment.update({"QQ_EMAIL": "", "QQ_EMAIL_AUTH_CODE": "", "PYTHONDONTWRITEBYTECODE": "1",
                            "QQMAIL_M5_ISOLATED_CHILD": "1"})
        self.assertNotIn("PYTHONPATH", environment)
        self.assertNotIn("PYTHONHOME", environment)
        self.assertEqual((environment["QQ_EMAIL"], environment["QQ_EMAIL_AUTH_CODE"]), ("", ""))
        probe = subprocess.run([sys.executable, "-B", "-c",
                                "import os,tempfile; assert not os.environ['QQ_EMAIL']; "
                                "assert not os.environ['QQ_EMAIL_AUTH_CODE']; "
                                "directory=tempfile.TemporaryDirectory(); directory.cleanup()"],
                               cwd=ROOT, text=True, capture_output=True, env=environment)
        self.assertEqual(probe.returncode, 0, (probe.stdout, probe.stderr))
        for module in modules:
            completed = subprocess.run([sys.executable, "-B", "-m", "unittest", f"tests.{module}", "-q"],
                                       cwd=ROOT, text=True, capture_output=True, env=environment)
            self.assertEqual(completed.returncode, 0, (module, completed.stdout, completed.stderr))


class M5StaticOwnershipTests(unittest.TestCase):
    def test_entrypoints_are_thin_and_do_not_own_protocol_or_mime_work(self):
        forbidden = {"imaplib", "smtplib", "ssl", "email", "os"}
        for name in ENTRYPOINTS:
            tree = ast.parse((SCRIPTS / name).read_text(encoding="utf-8"))
            imports = {
                alias.name.split(".")[0]
                for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names
            }
            imports.update(
                (node.module or "").split(".")[0]
                for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level == 0
            )
            self.assertFalse(imports & forbidden, name)
            self.assertFalse(any(
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"uid", "select", "list", "sendmail", "attach"}
                for node in ast.walk(tree)
            ), name)

    def test_authoritative_core_ownership_is_unambiguous(self):
        core = ROOT / "scripts" / "qqmail_core"
        text = {path.name: path.read_text(encoding="utf-8") for path in core.glob("*.py")}
        self.assertIn("import imaplib", text["connections.py"])
        self.assertIn("import smtplib", text["connections.py"])
        self.assertIn("import os", text["config.py"])
        self.assertEqual(sum("def safe_filename(" in value for value in text.values()), 1)
        self.assertEqual(sum("def build_draft(" in value for value in text.values()), 1)
        self.assertNotIn("BODY.PEEK[]", text["mutations.py"])
        self.assertIn("BODY.PEEK[HEADER.FIELDS", text["mutations.py"])

    def test_ast_rejects_legacy_sequence_operations_and_duplicate_owners(self):
        core = ROOT / "scripts" / "qqmail_core"
        text = {path.name: path.read_text(encoding="utf-8") for path in core.glob("*.py")}
        network_imports = {"imaplib", "smtplib", "ssl"}
        for path in (ROOT / "scripts").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported = {
                alias.name.split(".")[0]
                for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names
            }
            imported.update(
                (node.module or "").split(".")[0]
                for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level == 0
            )
            if path.name != "connections.py":
                self.assertFalse(imported & network_imports, path)
            if path.name != "config.py":
                self.assertFalse(any(
                    isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == "os" and node.attr == "environ"
                    for node in ast.walk(tree)
                ), path)
            self.assertFalse(any(
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "mail"
                and node.func.attr in {"search", "fetch", "store", "copy", "expunge"}
                for node in ast.walk(tree)
            ), path)
        self.assertEqual(sum("from email.header import decode_header" in value for value in text.values()), 1)
        self.assertEqual(sum("BODY.PEEK[]" in value for value in text.values()), 1)
        for name in ("attachments.py", "mutations.py"):
            self.assertNotIn("args.mail_ids.split", text[name])
            self.assertIn("parse_mailref_csv(", text[name])
        self.assertEqual(sum("def preview_text(" in value for value in text.values()), 0)
        self.assertNotIn("uid_fetch_exists", text["mutations.py"])
        self.assertNotIn("return destination, None", text["attachments.py"])
        self.assertNotIn("destination, _warning", text["attachments.py"])
        self.assertNotIn("item, _internal_date, part", text["readers.py"])


if __name__ == "__main__":
    unittest.main()
