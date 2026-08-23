"""Offline acceptance tests for the deliberately small M1 shared core."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests.support import BlockNetwork, FakeIMAP, FakeMailbox, FakeMessage, FakeSMTP, sample_message


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from qqmail_core.config import CredentialError, Credentials, load_credentials
from qqmail_core.confirmation import confirmation_matches, move_manifest, send_manifest
from qqmail_core.connections import imap_connection, smtp_connection, tls_context
from qqmail_core.folders import (FolderError, choose_trash_folder, decode_modified_utf7,
                                 encode_modified_utf7, parse_list_response, quote_mailbox)
from qqmail_core.mailref import MailRef, MailRefError, parse_uid, select_verified_mailref
from qqmail_core.results import EXIT_CODES, batch_result, emit_json, error_result


def load_list_script():
    spec = importlib.util.spec_from_file_location("m1_list_folders", SCRIPTS / "list_folders.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["m1_list_folders"] = module
    spec.loader.exec_module(module)
    return module


def load_script(name: str):
    module_name = "m1_" + name.replace(".py", "")
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


class ResultProtocolTests(OfflineTestCase):
    def test_statuses_batch_aggregation_json_and_exit_codes_are_canonical(self):
        self.assertEqual(batch_result(succeeded=["1"], failed=[])["status"], "success")
        self.assertEqual(batch_result(succeeded=[], failed=["1"])["status"], "error")
        partial = batch_result(succeeded=["1"], failed=["2"])
        self.assertEqual(partial["status"], "partial")
        self.assertEqual(EXIT_CODES, {"success": 0, "preview": 0, "partial": 2, "error": 1})
        output = io.StringIO()
        self.assertEqual(emit_json({"status": "preview", "message": "safe"}, stream=output), 0)
        self.assertEqual(output.getvalue().count("\n"), 1)
        self.assertEqual(error_result("bad input")["status"], "error")


class ConfigAndLifecycleTests(OfflineTestCase):
    def test_credentials_are_limited_to_supported_env_names_and_fail_before_connection(self):
        with self.assertRaises(CredentialError):
            load_credentials({})
        credentials = load_credentials({"QQ_EMAIL": "me@example.test", "QQ_EMAIL_AUTH_CODE": "token"})
        self.assertEqual(credentials.email, "me@example.test")
        self.assertEqual(credentials.auth_code, "token")

    def test_tls_context_timeout_and_cleanup_cover_success_and_exception_paths(self):
        context = tls_context()
        self.assertTrue(context.check_hostname)
        self.assertNotEqual(context.verify_mode, 0)
        credentials = Credentials("me@example.test", "token")
        imap = FakeIMAP()
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=imap) as constructor:
            with imap_connection(credentials, timeout=12.5):
                pass
        self.assertTrue(imap.logged_out)
        self.assertEqual(constructor.call_args.kwargs["timeout"], 12.5)

        failed_imap = FakeIMAP()
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=failed_imap):
            with self.assertRaises(RuntimeError):
                with imap_connection(credentials):
                    raise RuntimeError("test exception")
        self.assertTrue(failed_imap.logged_out)

        smtp = FakeSMTP()
        with patch("qqmail_core.connections.smtplib.SMTP", return_value=smtp) as smtp_constructor:
            with smtp_connection(credentials, timeout=9.0):
                pass
        self.assertTrue(smtp.closed)
        self.assertEqual(smtp_constructor.call_args.kwargs["timeout"], 9.0)
        self.assertEqual(smtp.log[1][0], "STARTTLS")

    def test_reply_reader_uses_shared_verified_imap_lifecycle(self):
        module = load_script("send_email.py")
        imap = FakeIMAP({"INBOX": FakeMailbox(messages=[FakeMessage("9", sample_message())])})
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=imap) as constructor:
            original = module.get_original_email("me@example.test", "token", "1", "INBOX")
        self.assertEqual(original["subject"], "Test")
        self.assertTrue(imap.logged_out)
        context = constructor.call_args.kwargs["ssl_context"]
        self.assertTrue(context.check_hostname)
        self.assertNotEqual(context.verify_mode, 0)
        self.assertIn("timeout", constructor.call_args.kwargs)


class FolderModelTests(OfflineTestCase):
    def test_modified_utf7_list_parsing_quoting_and_special_use_trash_are_stable(self):
        name = '项目 & "归档"'
        encoded = encode_modified_utf7(name)
        self.assertEqual(decode_modified_utf7(encoded), name)
        self.assertIn(",", encode_modified_utf7("台"))
        trash_name = "已删除邮件"
        folder = parse_list_response(f'(\\HasNoChildren \\Trash) "/" "{encode_modified_utf7(trash_name)}"')
        self.assertEqual(folder.display_name, trash_name)
        self.assertTrue(folder.is_trash)
        self.assertEqual(quote_mailbox('A "quote" \\ path'), '"A \\"quote\\" \\\\ path"')
        self.assertIs(choose_trash_folder([
            parse_list_response('(\\HasNoChildren) "/" "Trash"'), folder,
        ]), folder)

    def test_folder_controls_and_malformed_list_are_rejected(self):
        with self.assertRaises(FolderError):
            quote_mailbox("INBOX\r\nNOOP")
        with self.assertRaises(FolderError):
            parse_list_response('(\\HasNoChildren) "/" "unterminated')


class MailRefTests(OfflineTestCase):
    def test_uid_parser_rejects_sequence_sets_before_credentials_or_network(self):
        invalid = ("", "0", "-1", "1:*", "1:20", "1,2", " 1", "1 ", "4294967296", "1\n2")
        imap, smtp = FakeIMAP(), FakeSMTP()
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(MailRefError):
                    parse_uid(value)
                self.assertEqual(imap.log, [])
                self.assertEqual(smtp.log, [])
        self.assertEqual(parse_uid("4294967295"), "4294967295")
        with self.assertRaises(MailRefError):
            MailRef("INBOX", "1", "1:2")

    def test_uidvalidity_mismatch_stops_before_any_uid_operation(self):
        reference = MailRef("INBOX", "9", "88")
        imap = FakeIMAP({"INBOX": FakeMailbox(uidvalidity="10", messages=[
            FakeMessage("88", sample_message()),
        ])})
        with self.assertRaises(MailRefError):
            select_verified_mailref(imap, reference)
        self.assertEqual([call for call in imap.log if call[0] == "UID"], [])
        self.assertEqual([call for call in imap.log if call[0] in {"FETCH", "STORE", "COPY"}], [])


class ConfirmationTests(OfflineTestCase):
    def test_every_bound_move_or_send_field_change_invalidates_confirmation(self):
        first = MailRef("INBOX", "7", "2")
        move = move_manifest(action="move", source_folder="INBOX", destination_folder="Archive", references=[first])
        self.assertTrue(confirmation_matches(move, move["confirmation"]))
        changed_move = move_manifest(action="move", source_folder="INBOX", destination_folder="Other", references=[first])
        self.assertFalse(confirmation_matches(changed_move, move["confirmation"]))

        with tempfile.TemporaryDirectory() as directory:
            attachment = pathlib.Path(directory) / "note.txt"
            attachment.write_text("one", encoding="utf-8")
            send = send_manifest(account="me@example.test", to="to@example.test", cc=None, bcc=None,
                                 subject="subject", body="body", attachments=[attachment], reply_to=first)
            self.assertTrue(confirmation_matches(send, send["confirmation"]))
            attachment.write_text("two", encoding="utf-8")
            changed_send = send_manifest(account="me@example.test", to="to@example.test", cc=None, bcc=None,
                                         subject="subject", body="body", attachments=[attachment], reply_to=first)
        self.assertFalse(confirmation_matches(changed_send, send["confirmation"]))


class ListFoldersIntegrationTests(OfflineTestCase):
    def test_list_folders_uses_core_lifecycle_parser_and_result_protocol(self):
        module = load_list_script()
        imap = FakeIMAP({"INBOX": FakeMailbox(), "Trash": FakeMailbox()})
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=imap):
            result = module.list_folders("me@example.test", "token")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["total"], 2)
        self.assertEqual(set(result["folders"][0]), {"name", "display", "raw"})
        self.assertIn(("LOGOUT",), imap.log)

    def test_list_missing_credentials_outputs_exactly_one_json_document(self):
        module = load_list_script()
        stdout = io.StringIO()
        with patch.dict("os.environ", {}, clear=True), patch.object(sys, "argv", ["list_folders.py"]), \
                contextlib.redirect_stdout(stdout):
            code = module.main()
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue().count("\n"), 1)
