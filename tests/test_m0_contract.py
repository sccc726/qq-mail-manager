"""M0 contract tests: offline baseline features and future regression targets."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import socket
import sys
import unittest
from email.parser import BytesParser
from unittest.mock import patch

from tests.support import BlockNetwork, FakeIMAP, FakeMailbox, FakeMessage, FakeSMTP, NetworkBlocked, sample_message


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
ENTRYPOINTS = (
    "list_folders.py", "search_emails.py", "get_email.py", "download_attachment.py",
    "mark_email.py", "move_email.py", "send_email.py",
)


def load_script(name: str):
    module_name = "m0_" + name.replace(".py", "")
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / name)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        self.network = BlockNetwork()
        self.network.__enter__()
        self.addCleanup(self.network.__exit__, None, None, None)


class NetworkProtectionTests(OfflineTestCase):
    def test_unmocked_dns_call_fails_immediately(self):
        with self.assertRaises(NetworkBlocked):
            socket.getaddrinfo("imap.qq.com", 993)

    def test_unmocked_imap_constructor_fails_immediately(self):
        import imaplib
        with self.assertRaises(NetworkBlocked):
            imaplib.IMAP4_SSL("imap.qq.com", 993)


class FakeInfrastructureTests(OfflineTestCase):
    def test_fake_imap_tracks_uid_state_sequence_changes_and_phase_failures(self):
        imap = FakeIMAP({"INBOX": FakeMailbox(uidvalidity="44", messages=[
            FakeMessage("10", sample_message(subject="first")),
            FakeMessage("20", sample_message(subject="second")),
        ])}, failures={"fetch": [True, False]})
        self.assertEqual(imap.select("INBOX"), ("OK", [b"2"]))
        self.assertEqual(imap.response("UIDVALIDITY"), ("UIDVALIDITY", [b"44"]))
        self.assertIn("UIDPLUS", imap.capabilities)
        self.assertEqual(imap.fetch(b"1", "(BODY.PEEK[])" )[0], "NO")
        self.assertEqual(imap.fetch(b"1", "(BODY.PEEK[])" )[0], "OK")
        self.assertEqual(imap.store(b"1", "+FLAGS", "\\Deleted")[0], "OK")
        self.assertEqual(imap.expunge()[0], "OK")
        status, payload = imap.fetch(b"1", "(BODY.PEEK[])")
        self.assertEqual(status, "OK")
        self.assertIn(b"second", payload[0][1])
        self.assertEqual(imap.uid("FETCH", b"20", "(BODY.PEEK[])" )[0], "OK")

    def test_fake_smtp_tracks_tls_auth_envelope_rejections_and_cleanup(self):
        smtp = FakeSMTP(rejected={"no@example.test": (550, b"rejected")})
        with smtp as connection:
            connection.ehlo()
            connection.starttls(context="test-context")
            connection.login("me@example.test", "token")
            rejected = connection.sendmail("me@example.test", ["ok@example.test", "no@example.test"], "Subject: test\n\nbody")
        self.assertEqual(smtp.tls_context, "test-context")
        self.assertEqual(smtp.auth, ("me@example.test", "token"))
        self.assertEqual(rejected, {"no@example.test": (550, b"rejected")})
        self.assertEqual(smtp.envelopes[0][1], ["ok@example.test", "no@example.test"])
        self.assertTrue(smtp.closed)


class BaselineCliTests(OfflineTestCase):
    def test_all_entrypoints_offer_help_without_credentials_or_network(self):
        for name in ENTRYPOINTS:
            module = load_script(name)
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(sys, "argv", [name, "--help"]), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as result:
                    module.main()
            self.assertEqual(result.exception.code, 0, name)
            self.assertIn("usage:", stdout.getvalue().lower(), name)

    def test_missing_credentials_returns_one_parseable_json_document(self):
        invocations = (
            ("list_folders.py", []),
            ("search_emails.py", []),
            ("get_email.py", ["--mail_ids", "1", "--folder", "INBOX"]),
            ("download_attachment.py", ["--mail_ids", "1", "--folder", "INBOX"]),
            ("mark_email.py", ["--mail_ids", "1", "--action", "read", "--folder", "INBOX"]),
            ("move_email.py", ["--mail_ids", "1", "--src_folder", "INBOX", "--dst_folder", "Archive"]),
            ("send_email.py", ["--to", "friend@example.test", "--subject", "hello", "--body", "body"]),
        )
        env = os.environ.copy()
        env.pop("QQ_EMAIL", None)
        env.pop("QQ_EMAIL_AUTH_CODE", None)
        for name, arguments in invocations:
            module = load_script(name)
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, env, clear=True), patch.object(sys, "argv", [name, *arguments]), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                module.main()
            self.assertEqual(stderr.getvalue(), "", name)
            document = json.loads(stdout.getvalue())
            self.assertEqual(document["status"], "error", name)
            self.assertIn("凭证", document["message"], name)


class BehaviorFeatureTests(OfflineTestCase):
    def test_search_conditions_and_pagination_are_stable(self):
        module = load_script("search_emails.py")
        criteria, fuzzy = module.build_search_criteria(query="meeting", since="01-Jan-2024", seen=False)
        self.assertTrue(fuzzy)
        self.assertEqual(criteria, ["SINCE 01-Jan-2024", "UNSEEN"])

        messages = [FakeMessage(str(100 + index), sample_message(subject="meeting", body=str(index)))
                    for index in range(20)]
        imap = FakeIMAP({"INBOX": FakeMailbox(uidvalidity="77", messages=messages)})
        with patch.object(module.imaplib, "IMAP4_SSL", return_value=imap):
            result = module.query_emails("me@example.test", "token", query="meeting", limit=20)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["total_matched"], 20)
        self.assertEqual(result["total"], 15)
        self.assertTrue(result["has_more"])
        self.assertEqual(result["tip"], "还有更多结果，使用 --offset 15 查看下一页")
        self.assertEqual([item["mail_id"] for item in result["emails"]], [str(i) for i in range(20, 5, -1)])

    def test_basic_mime_parsing_keeps_body_and_attachment_metadata(self):
        module = load_script("get_email.py")
        message = BytesParser().parsebytes(sample_message(body="正文", attachment=("report.pdf", b"pdf")))
        body, attachments = module.extract_body_and_attachments(message)
        self.assertEqual(body.strip(), "正文")
        self.assertEqual(attachments, [{"name": "report.pdf", "type": "application/octet-stream"}])

    def test_reply_headers_and_envelope_are_constructed(self):
        module = load_script("send_email.py")
        smtp = FakeSMTP()
        with patch.object(module.smtplib, "SMTP", return_value=smtp):
            result = module.send_email("me@example.test", "token", "to@example.test", "Re: topic", "reply",
                                       cc="cc@example.test", bcc="bcc@example.test",
                                       in_reply_to="<original@example.test>", references="<thread@example.test>")
        self.assertEqual(result["status"], "success")
        self.assertEqual(smtp.envelopes[0][1], ["to@example.test", "cc@example.test", "bcc@example.test"])
        parsed = smtp.parsed_messages[0]
        self.assertEqual(parsed["In-Reply-To"], "<original@example.test>")
        self.assertEqual(parsed["References"], "<thread@example.test>")
        self.assertTrue(smtp.closed)

    def test_attachment_filename_is_cleaned_before_use(self):
        module = load_script("download_attachment.py")
        self.assertEqual(module.safe_filename("../../bad:report?.pdf"), "bad_report_.pdf")
        self.assertEqual(module.safe_filename("..."), "attachment")

    def test_move_preview_changes_no_server_state(self):
        module = load_script("move_email.py")
        imap = FakeIMAP({
            "INBOX": FakeMailbox(messages=[FakeMessage("9", sample_message(subject="keep"))]),
            "Archive": FakeMailbox(),
        })
        before = imap.snapshot()
        with patch.object(module.imaplib, "IMAP4_SSL", return_value=imap):
            result = module.move_emails("me@example.test", "token", ["1"], "INBOX", "Archive", confirm=False)
        self.assertEqual(result["status"], "preview")
        self.assertEqual(before, imap.snapshot())
        self.assertNotIn(("EXPUNGE",), imap.log)


class FutureRegressionTargets(OfflineTestCase):
    """Known defects remain visible but are not accepted as compatibility behavior."""

    @unittest.expectedFailure
    def test_uid_fetch_is_required_after_uid_migration(self):
        module = load_script("get_email.py")
        imap = FakeIMAP({"INBOX": FakeMailbox(messages=[FakeMessage("88", sample_message())])})
        with patch.object(module.imaplib, "IMAP4_SSL", return_value=imap):
            module.get_emails("me@example.test", "token", ["88"], "INBOX")
        self.assertTrue(any(call[:2] == ("UID", "FETCH") for call in imap.log))

    @unittest.expectedFailure
    def test_same_mail_id_in_two_folders_requires_distinct_mailrefs(self):
        module = load_script("search_emails.py")
        imap = FakeIMAP({
            "INBOX": FakeMailbox(uidvalidity="100", messages=[FakeMessage("9", sample_message(subject="same"))]),
            "Archive": FakeMailbox(uidvalidity="200", messages=[FakeMessage("9", sample_message(subject="same"))]),
        })
        with patch.object(module.imaplib, "IMAP4_SSL", return_value=imap):
            result = module.query_emails("me@example.test", "token", query="same", all_folders=True)
        self.assertEqual({(item["folder"], item["uidvalidity"], item["mail_id"]) for item in result["emails"]},
                         {("INBOX", "100", "9"), ("Archive", "200", "9")})

    @unittest.expectedFailure
    def test_search_select_failure_must_not_be_reported_as_empty_success(self):
        module = load_script("search_emails.py")
        imap = FakeIMAP({"INBOX": FakeMailbox()}, failures={"select": True})
        with patch.object(module.imaplib, "IMAP4_SSL", return_value=imap):
            result = module.query_emails("me@example.test", "token")
        self.assertEqual(result["status"], "error")

    @unittest.expectedFailure
    def test_confirmed_move_must_not_use_global_expunge(self):
        module = load_script("move_email.py")
        imap = FakeIMAP({
            "INBOX": FakeMailbox(messages=[FakeMessage("9", sample_message())]),
            "Archive": FakeMailbox(),
        })
        with patch.object(module.imaplib, "IMAP4_SSL", return_value=imap):
            module.move_emails("me@example.test", "token", ["1"], "INBOX", "Archive", confirm=True)
        self.assertNotIn(("EXPUNGE",), imap.log)

    @unittest.expectedFailure
    def test_send_requires_a_matching_confirmation_before_smtp_transfer(self):
        module = load_script("send_email.py")
        smtp = FakeSMTP()
        with patch.object(module.smtplib, "SMTP", return_value=smtp):
            module.send_email("me@example.test", "token", "to@example.test", "subject", "body")
        self.assertEqual(smtp.envelopes, [])
