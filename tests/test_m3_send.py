"""Offline M3 acceptance tests for preview-confirm SMTP delivery."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
import smtplib
from unittest.mock import patch

from tests.support import BlockNetwork, FakeIMAP, FakeMailbox, FakeMessage, FakeSMTP, sample_message


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def load_send():
    spec = importlib.util.spec_from_file_location("m3_send_email", SCRIPTS / "send_email.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["m3_send_email"] = module
    spec.loader.exec_module(module)
    return module


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        self.network = BlockNetwork()
        self.network.__enter__()
        self.addCleanup(self.network.__exit__, None, None, None)

    @staticmethod
    def _run(module, arguments, *, env=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        values = {"QQ_EMAIL": "me@example.test", "QQ_EMAIL_AUTH_CODE": "token"} if env is None else env
        with patch.dict(os.environ, values, clear=True), patch.object(sys, "argv", ["send_email.py", *arguments]), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = module.main()
        assert stderr.getvalue() == ""
        assert len(stdout.getvalue().splitlines()) == 1
        return code, json.loads(stdout.getvalue())


class RecipientAndPreviewTests(OfflineTestCase):
    def test_preview_normalizes_deduplicates_and_never_opens_smtp(self):
        module, smtp = load_send(), FakeSMTP()
        with patch("qqmail_core.connections.smtplib.SMTP", return_value=smtp):
            preview = module.send_email("me@example.test", "token", "Alice <TO@example.test>, to@example.test",
                                        "subject", "body", cc="TO@example.test, cc@example.test",
                                        bcc="cc@example.test, bcc@example.test")
        self.assertEqual(preview["status"], "preview")
        self.assertEqual(preview["to"], ["Alice <TO@example.test>", "to@example.test"])
        self.assertEqual(preview["cc"], ["cc@example.test"])
        self.assertEqual(preview["bcc"], ["bcc@example.test"])
        self.assertEqual(preview["envelope_recipients"], ["TO@example.test", "to@example.test", "cc@example.test", "bcc@example.test"])
        self.assertEqual(smtp.log, [])
        self.assertIn("sha256", preview["body_summary"])

    def test_confirmed_send_has_bcc_only_in_envelope_and_uses_one_transfer(self):
        module, smtp = load_send(), FakeSMTP()
        preview = module.send_email("me@example.test", "token", "to@example.test", "subject", "body",
                                    cc="cc@example.test", bcc="bcc@example.test")
        with patch("qqmail_core.connections.smtplib.SMTP", return_value=smtp):
            result = module.send_email("me@example.test", "token", "to@example.test", "subject", "body",
                                       cc="cc@example.test", bcc="bcc@example.test",
                                       confirmation=preview["confirmation"])
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(smtp.envelopes), 1)
        self.assertEqual(smtp.envelopes[0][1], ["to@example.test", "cc@example.test", "bcc@example.test"])
        parsed = smtp.parsed_messages[0]
        self.assertIsNone(parsed["Bcc"])
        self.assertEqual(parsed["Cc"], "cc@example.test")
        self.assertTrue(smtp.closed)

    def test_invalid_recipient_and_confirmation_mismatch_fail_before_smtp(self):
        module, smtp = load_send(), FakeSMTP()
        invalid = module.send_email("me@example.test", "token", "not-an-address", "subject", "body")
        self.assertEqual(invalid["status"], "error")
        preview = module.send_email("me@example.test", "token", "to@example.test", "subject", "body")
        with patch("qqmail_core.connections.smtplib.SMTP", return_value=smtp):
            mismatch = module.send_email("me@example.test", "token", "other@example.test", "subject", "body",
                                         confirmation=preview["confirmation"])
        self.assertEqual(mismatch["code"], "confirmation_mismatch")
        self.assertEqual(smtp.log, [])

    def test_address_parser_handles_quoted_commas_and_rejects_empty_controls_and_over_limit(self):
        module = load_send()
        parsed = module.parse_recipients('"Doe, Jane" <jane@example.test>, john@example.test', label="收件人")
        self.assertEqual([item.address for item in parsed], ["jane@example.test", "john@example.test"])
        for bad in ("a@example.test,,b@example.test", "a@example.test,", "a@example.test\r\nb@example.test",
                    "a@example.test\x00"):
            with self.subTest(bad=bad):
                with self.assertRaises(module.SendInputError):
                    module.parse_recipients(bad, label="收件人")
        too_many = ",".join(f"person{index}@example.test" for index in range(module.MAX_RECIPIENTS + 1))
        with self.assertRaises(module.SendInputError):
            module.normalize_recipient_groups(too_many, None, None)

    def test_strict_ascii_addr_spec_and_domain_rules_block_preview(self):
        module, smtp = load_send(), FakeSMTP()
        for bad in ("Alice <victim@example.test", '"Alice <victim@example.test>', "a@example..test",
                    "a@-example.test", "a@example-.test", "用户@example.test", "a@例子.test"):
            with self.subTest(bad=bad), patch("qqmail_core.connections.smtplib.SMTP", return_value=smtp):
                result = module.send_email("me@example.test", "token", bad, "subject", "body")
            self.assertEqual(result["status"], "error")
        accepted = module.parse_recipients('名字 <name@example.test>', label="收件人")
        self.assertEqual(accepted[0].address, "name@example.test")
        self.assertEqual(smtp.log, [])

    def test_addr_spec_length_boundaries_are_checked_before_preview(self):
        module = load_send()
        domain = "a" * 63 + "." + "b" * 63 + "." + "c" * 63 + "." + "d" * 60
        self.assertEqual(len(domain), 252)
        self.assertTrue(module._valid_address("a@" + domain))
        self.assertFalse(module._valid_address("a@" + domain + "x"))
        self.assertFalse(module._valid_address("a" * 65 + "@example.test"))
        self.assertFalse(module._valid_address("a" * 64 + "@" + domain))

    def test_local_part_case_is_distinct_but_domain_case_is_deduplicated(self):
        module = load_send()
        _to, _cc, _bcc, envelope = module.normalize_recipient_groups(
            "User@EXAMPLE.test,user@example.test", "User@example.TEST", None)
        self.assertEqual(envelope, ("User@EXAMPLE.test", "user@example.test"))

    def test_subject_body_and_attachment_files_are_bound_to_confirmation(self):
        module, smtp = load_send(), FakeSMTP()
        with tempfile.TemporaryDirectory() as directory:
            folder = pathlib.Path(directory)
            subject_file, body_file, attachment = folder / "subject.txt", folder / "body.txt", folder / "a.bin"
            subject_file.write_text("Subject", encoding="utf-8")
            body_file.write_text("Body", encoding="utf-8")
            attachment.write_bytes(b"one")
            source = module.DraftInput("to@example.test", None, None, "Subject", "Body", False,
                                       (str(attachment),), str(subject_file), str(body_file))
            preview = module.build_draft("me@example.test", source)
            self.assertEqual(preview.manifest["attachments"][0]["type"], "regular")
            self.assertIn("mtime_ns", preview.manifest["body_file"])
            attachment.write_bytes(b"two")
            with patch("qqmail_core.connections.smtplib.SMTP", return_value=smtp):
                result = module.send_email("me@example.test", "token", "to@example.test", "Subject", "Body",
                                           attachments=[str(attachment)], subject_file=str(subject_file),
                                           body_file=str(body_file), confirmation=preview.manifest["confirmation"])
        self.assertEqual(result["code"], "confirmation_mismatch")
        self.assertEqual(smtp.log, [])

    def test_file_snapshot_uses_the_same_bytes_for_manifest_and_mime(self):
        module, smtp = load_send(), FakeSMTP()
        with tempfile.TemporaryDirectory() as directory:
            attachment = pathlib.Path(directory) / "a.bin"
            attachment.write_bytes(b"original")
            source = module.DraftInput("to@example.test", None, None, "subject", "body", False, (str(attachment),))
            draft = module.build_draft("me@example.test", source)
            attachment.write_bytes(b"changed!")
            with patch("qqmail_core.connections.smtplib.SMTP", return_value=smtp):
                result = module.transmit_draft(module.Credentials("me@example.test", "token"), draft)
        self.assertEqual(result["status"], "success")
        parsed = smtp.parsed_messages[0]
        attached = next(part for part in parsed.walk() if part.get_filename() == "a.bin")
        self.assertEqual(attached.get_payload(decode=True), b"original")
        self.assertEqual(draft.manifest["attachments"][0]["sha256"], __import__("hashlib").sha256(b"original").hexdigest())

    def test_thread_headers_are_validated_and_bound_by_confirmation(self):
        module, smtp = load_send(), FakeSMTP()
        preview = module.send_email("me@example.test", "token", "to@example.test", "subject", "body",
                                    in_reply_to="<one@example.test>", references="<root@example.test> <one@example.test>")
        with patch("qqmail_core.connections.smtplib.SMTP", return_value=smtp):
            changed = module.send_email("me@example.test", "token", "to@example.test", "subject", "body",
                                        in_reply_to="<other@example.test>", references="<root@example.test> <one@example.test>",
                                        confirmation=preview["confirmation"])
        self.assertEqual(changed["code"], "confirmation_mismatch")
        self.assertEqual(smtp.log, [])
        invalid = module.send_email("me@example.test", "token", "to@example.test", "subject", "body",
                                    in_reply_to="not-a-message-id")
        self.assertEqual(invalid["code"], "invalid_send_input")


class TransportTests(OfflineTestCase):
    def _confirmed(self, rejected=None, failures=None):
        module, smtp = load_send(), FakeSMTP(rejected=rejected, failures=failures)
        preview = module.send_email("me@example.test", "token", "ok@example.test, no@example.test", "subject", "body")
        with patch("qqmail_core.connections.smtplib.SMTP", return_value=smtp):
            result = module.send_email("me@example.test", "token", "ok@example.test, no@example.test", "subject", "body",
                                       confirmation=preview["confirmation"])
        return result, smtp

    def test_sendmail_refusal_dictionary_maps_success_partial_and_error(self):
        success, success_smtp = self._confirmed()
        self.assertEqual(success["status"], "success")
        self.assertEqual(success["success"], ["ok@example.test", "no@example.test"])
        partial, partial_smtp = self._confirmed({"no@example.test": (550, b"mailbox unavailable")})
        self.assertEqual(partial["status"], "partial")
        self.assertEqual(partial["failed"][0], {"recipient": "no@example.test", "reason": "mailbox unavailable", "code": "550"})
        rejected = {"ok@example.test": (550, b"rejected"), "no@example.test": (551, b"no user")}
        error, error_smtp = self._confirmed(rejected)
        self.assertEqual(error["status"], "error")
        self.assertEqual(error["success"], [])
        self.assertEqual([item["recipient"] for item in error["failed"]], ["ok@example.test", "no@example.test"])
        for smtp in (success_smtp, partial_smtp, error_smtp):
            self.assertEqual(len(smtp.envelopes), 1)
            self.assertTrue(smtp.closed)

    def test_multiline_refusal_reasons_are_safely_single_line_for_dict_and_exception(self):
        module = load_send()
        response = {"no@example.test": (550, b"first line\nsecond\tline\xff")}
        partial, _smtp = self._confirmed(response)
        self.assertEqual(partial["status"], "partial")
        self.assertEqual(partial["failed"][0]["reason"], "first line second line�")
        smtp = FakeSMTP()
        def all_refused(_sender, recipients, _mime):
            raise smtplib.SMTPRecipientsRefused({address: (550, "line one\r\nline two\tend") for address in recipients})
        smtp.sendmail = all_refused
        preview = module.send_email("me@example.test", "token", "only@example.test", "subject", "body")
        with patch("qqmail_core.connections.smtplib.SMTP", return_value=smtp):
            result = module.send_email("me@example.test", "token", "only@example.test", "subject", "body",
                                       confirmation=preview["confirmation"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["failed"][0]["reason"], "line one line two end")
        self.assertFalse(result.get("delivery_indeterminate", False))

    def test_recipients_refused_requires_complete_envelope_coverage(self):
        module = load_send()
        for response in ({}, {"ok@example.test": (550, b"no")},
                         {"ok@example.test": (550, b"no"), "no@example.test": (551, b"no"),
                          "extra@example.test": (550, b"extra")}, None):
            with self.subTest(response=response):
                smtp = FakeSMTP()
                smtp.sendmail = lambda *_args, value=response: (_ for _ in ()).throw(smtplib.SMTPRecipientsRefused(value))
                preview = module.send_email("me@example.test", "token", "ok@example.test,no@example.test", "subject", "body")
                with patch("qqmail_core.connections.smtplib.SMTP", return_value=smtp):
                    result = module.send_email("me@example.test", "token", "ok@example.test,no@example.test", "subject", "body",
                                               confirmation=preview["confirmation"])
                self.assertEqual(result["code"], "smtp_invalid_response")
                self.assertTrue(result["delivery_indeterminate"])

    def test_recipients_refused_and_post_send_exception_are_error_not_false_safe(self):
        module = load_send()
        refusal = FakeSMTP()
        def reject_all(_sender, recipients, _mime):
            refusal.log.append(("SENDMAIL", _sender, list(recipients), _mime))
            raise smtplib.SMTPRecipientsRefused({address: (550, b"refused") for address in recipients})
        refusal.sendmail = reject_all
        preview = module.send_email("me@example.test", "token", "a@example.test", "subject", "body")
        with patch("qqmail_core.connections.smtplib.SMTP", return_value=refusal):
            result = module.send_email("me@example.test", "token", "a@example.test", "subject", "body",
                                       confirmation=preview["confirmation"])
        self.assertEqual(result["status"], "error")
        self.assertFalse(result.get("delivery_indeterminate", False))
        indeterminate, smtp = self._confirmed(failures={"sendmail": True})
        self.assertEqual(indeterminate["status"], "error")
        self.assertTrue(indeterminate["delivery_indeterminate"])
        self.assertTrue(smtp.closed)

    def test_invalid_sendmail_responses_are_indeterminate_structured_errors(self):
        module = load_send()
        invalid_responses = (None, ["not a mapping"], {"unknown@example.test": (550, b"no")},
                             {"ok@example.test": (550, b"no"), "ok@EXAMPLE.test": (551, b"dup")},
                             {"ok@example.test": ("550", b"bad code")})
        for response in invalid_responses:
            with self.subTest(response=response):
                smtp = FakeSMTP()
                smtp.rejected = response
                preview = module.send_email("me@example.test", "token", "ok@example.test", "subject", "body")
                with patch("qqmail_core.connections.smtplib.SMTP", return_value=smtp):
                    result = module.send_email("me@example.test", "token", "ok@example.test", "subject", "body",
                                               confirmation=preview["confirmation"])
                self.assertEqual(result["code"], "smtp_invalid_response")
                self.assertTrue(result["delivery_indeterminate"])
                self.assertEqual(len([call for call in smtp.log if call[0] == "SENDMAIL"]), 1)
        refused = FakeSMTP()
        refused.sendmail = lambda *_args: (_ for _ in ()).throw(smtplib.SMTPRecipientsRefused(None))
        preview = module.send_email("me@example.test", "token", "ok@example.test", "subject", "body")
        with patch("qqmail_core.connections.smtplib.SMTP", return_value=refused):
            result = module.send_email("me@example.test", "token", "ok@example.test", "subject", "body",
                                       confirmation=preview["confirmation"])
        self.assertEqual(result["code"], "smtp_invalid_response")
        self.assertTrue(result["delivery_indeterminate"])

    def test_send_stage_auth_exception_is_indeterminate_but_login_auth_is_not(self):
        module = load_send()
        smtp = FakeSMTP()
        def send_auth(_sender, _recipients, _mime):
            raise smtplib.SMTPAuthenticationError(535, b"late auth failure")
        smtp.sendmail = send_auth
        preview = module.send_email("me@example.test", "token", "ok@example.test", "subject", "body")
        with patch("qqmail_core.connections.smtplib.SMTP", return_value=smtp):
            late = module.send_email("me@example.test", "token", "ok@example.test", "subject", "body",
                                     confirmation=preview["confirmation"])
        self.assertEqual(late["code"], "smtp_authentication_failed")
        self.assertTrue(late["delivery_indeterminate"])
        early, _smtp = self._confirmed(failures={"login": True})
        self.assertFalse(early.get("delivery_indeterminate", False))

    def test_tls_auth_and_safe_test_connection_never_send(self):
        module = load_send()
        for failures in ({"tls": True}, {"login": True}):
            with self.subTest(failures=failures):
                result, smtp = self._confirmed(failures=failures)
                self.assertEqual(result["status"], "error")
                self.assertEqual(smtp.envelopes, [])
                self.assertTrue(smtp.closed)
        smtp = FakeSMTP()
        with patch("qqmail_core.connections.smtplib.SMTP", return_value=smtp):
            result = module.test_smtp(module.Credentials("me@example.test", "token"))
        self.assertEqual(result["status"], "success")
        self.assertFalse(result["sent"])
        self.assertFalse(any(call[0] == "SENDMAIL" for call in smtp.log))
        self.assertTrue(smtp.closed)

    def test_quit_failure_falls_back_to_close_without_overwriting_delivery_result(self):
        result, smtp = self._confirmed(failures={"quit": True})
        self.assertEqual(result["status"], "success")
        self.assertIn(("QUIT",), smtp.log)
        self.assertIn(("CLOSE",), smtp.log)
        self.assertTrue(smtp.closed)


class ReplyAndCliTests(OfflineTestCase):
    def test_cli_exit_codes_and_confirmation_pairing_are_exact(self):
        module, smtp = load_send(), FakeSMTP(rejected={"no@example.test": (550, b"rejected")})
        args = ["--to", "ok@example.test,no@example.test", "--subject", "subject", "--body", "body"]
        preview_code, preview = self._run(module, args)
        self.assertEqual(preview_code, 0)
        with patch("qqmail_core.connections.smtplib.SMTP", return_value=smtp):
            partial_code, partial = self._run(module, [*args, "--confirm", "--confirmation", preview["confirmation"]])
        self.assertEqual((partial_code, partial["status"]), (2, "partial"))
        self.assertEqual(len(smtp.envelopes), 1)
        for incomplete in ([*args, "--confirm"], [*args, "--confirmation", preview["confirmation"]],
                           [*args, "--confirmation", ""], [*args, "--confirm", "--confirmation", "wrong"]):
            with self.subTest(arguments=incomplete):
                blocked = FakeSMTP()
                with patch("qqmail_core.connections.smtplib.SMTP", return_value=blocked):
                    code, result = self._run(module, incomplete)
                self.assertEqual((code, result["status"]), (1, "error"))
                self.assertEqual(blocked.log, [])

    def test_static_reply_body_and_sender_validation_precede_credentials_or_network(self):
        module, smtp = load_send(), FakeSMTP()
        reply = ["--reply-to-id", "9", "--reply-folder", "INBOX", "--reply-uidvalidity", "1"]
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=FakeIMAP()), \
                patch("qqmail_core.connections.smtplib.SMTP", return_value=smtp):
            code, result = self._run(module, reply, env={})
        self.assertEqual((code, result["code"]), (1, "invalid_arguments"))
        self.assertEqual(smtp.log, [])
        with tempfile.TemporaryDirectory() as directory:
            body_file = pathlib.Path(directory) / "empty.txt"
            body_file.write_text("", encoding="utf-8")
            code, result = self._run(module, [*reply, "--body-file", str(body_file)], env={})
        self.assertEqual((code, result["code"]), (1, "invalid_arguments"))
        bad_sender_env = {"QQ_EMAIL": "not-an-address", "QQ_EMAIL_AUTH_CODE": "token"}
        with patch("qqmail_core.connections.smtplib.SMTP", return_value=smtp):
            code, result = self._run(module, ["--to", "to@example.test", "--subject", "s", "--body", "b"],
                                     env=bad_sender_env)
        self.assertEqual((code, result["code"]), (1, "invalid_arguments"))
        self.assertEqual(smtp.log, [])
        reply_imap = FakeIMAP({"INBOX": FakeMailbox(messages=[FakeMessage("9", sample_message())])})
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=reply_imap), \
                patch("qqmail_core.connections.smtplib.SMTP", return_value=smtp):
            code, result = self._run(module, ["--reply-to-id", "9", "--reply-folder", "INBOX",
                                              "--reply-uidvalidity", "1", "--body", "reply"], env=bad_sender_env)
        self.assertEqual((code, result["code"]), (1, "invalid_arguments"))
        self.assertEqual(reply_imap.log, [])
        self.assertEqual(smtp.log, [])

    def test_test_mode_rejects_html_and_does_not_read_message_inputs(self):
        module, smtp = load_send(), FakeSMTP()
        with patch("qqmail_core.connections.smtplib.SMTP", return_value=smtp):
            code, result = self._run(module, ["--test", "--html"])
        self.assertEqual((code, result["code"]), (1, "invalid_arguments"))
        self.assertEqual(smtp.log, [])

    def test_test_mode_detects_explicit_empty_send_options_and_invalid_sender_before_smtp(self):
        module = load_send()
        empty_options = ("--to", "--cc", "--bcc", "--subject", "--subject-file", "--body", "--body-file", "--attachments")
        for option in empty_options:
            with self.subTest(option=option):
                smtp = FakeSMTP()
                with patch("qqmail_core.connections.smtplib.SMTP", return_value=smtp):
                    code, result = self._run(module, ["--test", option, ""])
                self.assertEqual((code, result["code"]), (1, "invalid_arguments"))
                self.assertEqual(smtp.log, [])
        smtp = FakeSMTP()
        with patch("qqmail_core.connections.smtplib.SMTP", return_value=smtp):
            code, result = self._run(module, ["--test"], env={"QQ_EMAIL": "bad", "QQ_EMAIL_AUTH_CODE": "token"})
        self.assertEqual((code, result["code"]), (1, "invalid_sender"))
        self.assertEqual(smtp.log, [])

    def test_explicit_blank_recipients_are_errors_not_absent_fields(self):
        module, smtp = load_send(), FakeSMTP()
        with patch("qqmail_core.connections.smtplib.SMTP", return_value=smtp):
            code, result = self._run(module, ["--to", " ", "--cc", "cc@example.test", "--subject", "s", "--body", "b"])
        self.assertEqual((code, result["code"]), (1, "invalid_arguments"))
        self.assertEqual(smtp.log, [])
        reply = ["--reply-to-id", "9", "--reply-folder", "INBOX", "--reply-uidvalidity", "1", "--to", " ", "--body", "b"]
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=FakeIMAP()), \
                patch("qqmail_core.connections.smtplib.SMTP", return_value=smtp):
            code, result = self._run(module, reply)
        self.assertEqual((code, result["code"]), (1, "invalid_arguments"))
        self.assertEqual(smtp.log, [])

    def test_reply_uidvalidity_and_source_changes_stop_before_smtp(self):
        module, smtp = load_send(), FakeSMTP()
        imap = FakeIMAP({"INBOX": FakeMailbox(uidvalidity="1", messages=[FakeMessage("9", sample_message())])})
        args = ["--reply-to-id", "9", "--reply-folder", "INBOX", "--reply-uidvalidity", "1", "--body", "reply"]
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=imap):
            code, preview = self._run(module, args)
        self.assertEqual(code, 0)
        changed = sample_message(subject="changed")
        imap.folders["INBOX"].messages[0].raw = changed
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=imap), \
                patch("qqmail_core.connections.smtplib.SMTP", return_value=smtp):
            code, result = self._run(module, [*args, "--confirm", "--confirmation", preview["confirmation"]])
        self.assertNotEqual(code, 0)
        self.assertEqual(result["code"], "confirmation_mismatch")
        self.assertEqual(smtp.log, [])
        imap.folders["INBOX"].uidvalidity = "2"
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=imap), \
                patch("qqmail_core.connections.smtplib.SMTP", return_value=smtp):
            code, result = self._run(module, [*args, "--confirm", "--confirmation", preview["confirmation"]])
        self.assertNotEqual(code, 0)
        self.assertEqual(result["code"], "invalid_mailref")
        self.assertEqual(smtp.log, [])

    def test_cli_argument_errors_precede_credentials_and_test_mode_is_non_sending(self):
        module, smtp = load_send(), FakeSMTP()
        code, result = self._run(module, ["--to", "bad", "--subject", "x", "--body", "y"], env={})
        self.assertNotEqual(code, 0)
        self.assertEqual(result["code"], "invalid_arguments")
        with patch("qqmail_core.connections.smtplib.SMTP", return_value=smtp):
            code, result = self._run(module, ["--test"])
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "success")
        self.assertFalse(any(call[0] == "SENDMAIL" for call in smtp.log))

    def test_reply_rejects_invalid_thread_headers_before_smtp(self):
        module, smtp = load_send(), FakeSMTP()
        raw = sample_message().replace(b"Message-ID: <sample@example.test>", b"Message-ID: invalid\r\n")
        imap = FakeIMAP({"INBOX": FakeMailbox(messages=[FakeMessage("9", raw)])})
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=imap), \
                patch("qqmail_core.connections.smtplib.SMTP", return_value=smtp):
            code, result = self._run(module, ["--reply-to-id", "9", "--reply-folder", "INBOX",
                                              "--reply-uidvalidity", "1", "--body", "reply"])
        self.assertNotEqual(code, 0)
        self.assertEqual(result["code"], "reply_source_unavailable")
        self.assertEqual(smtp.log, [])

    def test_folded_reply_address_is_safely_unfolded_and_quote_without_body_fails_safely(self):
        module, smtp = load_send(), FakeSMTP()
        raw = sample_message().replace(b"From: alice@example.test", b"From: Very Long Display\r\n Name <alice@example.test>")
        imap = FakeIMAP({"INBOX": FakeMailbox(messages=[FakeMessage("9", raw)])})
        args = ["--reply-to-id", "9", "--reply-folder", "INBOX", "--reply-uidvalidity", "1", "--body", "reply"]
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=imap):
            code, preview = self._run(module, args)
        self.assertEqual((code, preview["status"]), (0, "preview"))
        empty_raw = (b"From: alice@example.test\r\nMessage-ID: <empty@example.test>\r\n"
                     b"Subject: Empty\r\n\r\n")
        empty_imap = FakeIMAP({"INBOX": FakeMailbox(messages=[FakeMessage("9", empty_raw)])})
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=empty_imap), \
                patch("qqmail_core.connections.smtplib.SMTP", return_value=smtp):
            code, result = self._run(module, ["--reply-to-id", "9", "--reply-folder", "INBOX",
                                              "--reply-uidvalidity", "1", "--reply-quote"])
        self.assertEqual((code, result["code"]), (1, "invalid_arguments"))
        self.assertEqual(smtp.log, [])

    def test_folded_and_tabbed_reply_subject_builds_safe_preview(self):
        module = load_send()
        raw = sample_message().replace(b"Subject: Test", b"Subject: Long\r\n subject\twith tab")
        imap = FakeIMAP({"INBOX": FakeMailbox(messages=[FakeMessage("9", raw)])})
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=imap):
            code, preview = self._run(module, ["--reply-to-id", "9", "--reply-folder", "INBOX",
                                              "--reply-uidvalidity", "1", "--body", "reply"])
        self.assertEqual((code, preview["status"]), (0, "preview"))
        self.assertEqual(preview["subject"], "Re: Long subject with tab")
