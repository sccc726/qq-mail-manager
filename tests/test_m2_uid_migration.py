"""M2 end-to-end UID migration acceptance tests; entirely offline."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests.support import BlockNetwork, FakeIMAP, FakeMailbox, FakeMessage, sample_message

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from qqmail_core.imap_uid import select_uid_fetch, uid_fetch_exists


def load(name):
    module_name = "m2_" + name.replace(".py", "")
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


class FakeImapUidStateTests(OfflineTestCase):
    def test_uid_fetch_metadata_tracks_reordered_sequence_and_copy_assigns_target_uid(self):
        imap = FakeIMAP({"INBOX": FakeMailbox(uidvalidity="7", messages=[
            FakeMessage("8", sample_message(subject="old"), internaldate="01-Jan-2024 01:00:00 +0000"),
            FakeMessage("9", sample_message(subject="kept"), internaldate="02-Jan-2024 02:00:00 +0000"),
        ]), "Archive": FakeMailbox(messages=[FakeMessage("20", sample_message())])}, capabilities=("UIDPLUS",))
        imap.select("INBOX")
        imap.store("1", "+FLAGS", "\\Deleted")
        imap.expunge()
        status, data = imap.uid("FETCH", "9", "(BODY.PEEK[] INTERNALDATE)")
        self.assertEqual(status, "OK")
        self.assertIn(b"1 (UID 9 INTERNALDATE \"02-Jan-2024 02:00:00 +0000\"", data[0][0])
        self.assertEqual(imap.uid("COPY", "9", "Archive")[0], "OK")
        self.assertEqual(imap.folders["Archive"].messages[-1].uid, "21")
        self.assertEqual(imap.uid("FETCH", "404", "(BODY.PEEK[])"), ("NO", [b"not found"]))

    def test_folder_command_uid_failure_injection_is_isolated(self):
        imap = FakeIMAP({"INBOX": FakeMailbox(messages=[FakeMessage("9", sample_message())]),
                         "Archive": FakeMailbox(messages=[FakeMessage("9", sample_message())])},
                        failures={("fetch", "Archive", "9"): True})
        imap.select("INBOX")
        self.assertEqual(imap.uid("FETCH", "9", "(BODY.PEEK[])" )[0], "OK")
        imap.select("Archive")
        self.assertEqual(imap.uid("FETCH", "9", "(BODY.PEEK[])" )[0], "NO")


class UidReadAndSearchTests(OfflineTestCase):
    def test_readers_use_uid_after_expunge_and_return_complete_references(self):
        get = load("get_email.py")
        download = load("download_attachment.py")
        box = FakeMailbox(uidvalidity="3", messages=[
            FakeMessage("10", sample_message(subject="discard")),
            FakeMessage("20", sample_message(subject="wanted", attachment=("a.txt", b"a"))),
        ])
        imap = FakeIMAP({"INBOX": box})
        imap.select("INBOX"); imap.store("1", "+FLAGS", "\\Deleted"); imap.expunge()
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=imap):
            result = get.get_emails("me@example.test", "token", ["20"], "INBOX", "3")
            self.assertEqual(result["emails"][0]["subject"], "wanted")
            with self.subTest("attachment"):
                with __import__("tempfile").TemporaryDirectory() as directory:
                    attached = download.download_attachments("me@example.test", "token", ["20"], "INBOX", "3", directory)
                self.assertEqual(attached["success"][0]["mail_id"], "20")
        self.assertTrue(all(call[0] != "FETCH" for call in imap.log))
        self.assertTrue(any(call[:2] == ("UID", "FETCH") for call in imap.log))
        self.assertEqual(set(result["emails"][0]).issuperset({"folder", "uidvalidity", "mail_id"}), True)

    def test_empty_uid_fetch_and_bad_internaldate_are_failures_not_empty_success(self):
        get, search = load("get_email.py"), load("search_emails.py")
        imap = FakeIMAP({"INBOX": FakeMailbox(uidvalidity="1", messages=[FakeMessage("9", sample_message())])},
                        failures={("uid_fetch_empty", "INBOX", "9"): True})
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=imap):
            result = get.get_emails("me@example.test", "token", ["9"], "INBOX", "1")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["failed"][0]["mail_id"], "9")
        aware = search._internaldate(b'1 (INTERNALDATE "01-Jan-2024 10:00:00 +0800")')
        self.assertIsNotNone(aware.tzinfo)
        with self.assertRaises(ValueError):
            search._internaldate(b"1 (BODY[])")
        with self.assertRaises(ValueError):
            search._internaldate(b'1 (INTERNALDATE "not-a-date")')

    def test_query_uses_uid_search_and_internaldate_global_sort_recent_filter_and_partial(self):
        search = load("search_emails.py")
        now = datetime(2024, 1, 3, 12, tzinfo=timezone.utc)
        imap = FakeIMAP({
            "INBOX": FakeMailbox(uidvalidity="1", messages=[
                FakeMessage("9", sample_message(subject='雪 "q" \\'), internaldate="03-Jan-2024 11:30:00 +0000"),
            ]),
            "Archive": FakeMailbox(uidvalidity="2", messages=[
                FakeMessage("9", sample_message(subject="older"), internaldate="03-Jan-2024 10:30:00 +0000"),
                FakeMessage("10", sample_message(subject="too old"), internaldate="01-Jan-2024 10:30:00 +0000"),
            ]),
        })
        criteria, fuzzy = search.build_search_criteria(query='雪 "q" \\')
        self.assertEqual(criteria, [])
        self.assertEqual(fuzzy[0], 'SUBJECT "雪 \\"q\\" ' + "\\\\" + '"')
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=imap):
            result = search.query_emails("me@example.test", "token", query="*", all_folders=True,
                                         recent="2h", limit=20, now=now)
        self.assertEqual(result["status"], "success")
        self.assertEqual([(item["folder"], item["uidvalidity"], item["mail_id"]) for item in result["emails"]],
                         [("INBOX", "1", "9"), ("Archive", "2", "9")])
        self.assertTrue(any(call[:2] == ("UID", "SEARCH") for call in imap.log))
        self.assertTrue(all(call[0] != "SEARCH" and call[0] != "FETCH" for call in imap.log))

        partial = FakeIMAP({"INBOX": FakeMailbox(uidvalidity="1", messages=[
            FakeMessage("1", sample_message()), FakeMessage("2", sample_message())])},
            failures={("fetch", "INBOX", "2"): True})
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=partial):
            result = search.query_emails("me@example.test", "token")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["failed"][0]["mail_id"], "2")

    def test_query_controls_and_mailref_validation_happen_before_credentials_or_network(self):
        search, mark = load("search_emails.py"), load("mark_email.py")
        with self.assertRaises(ValueError):
            search.build_search_criteria(query="bad\r\nNOOP")
        stdout = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch.object(sys, "argv", ["mark_email.py", "--mail_ids", "1:*", "--action", "read", "--folder", "INBOX", "--uidvalidity", "1"]), contextlib.redirect_stdout(stdout):
            self.assertEqual(mark.main(), 1)
        self.assertEqual(json.loads(stdout.getvalue())["code"], "invalid_mailref")


class UidMutationTests(OfflineTestCase):
    def _preview_and_confirm(self, module, imap):
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=imap):
            preview = module.move_emails("me@example.test", "token", ["9"], "INBOX", "Archive",
                                         uidvalidity="1")
            return module.move_emails("me@example.test", "token", ["9"], "INBOX", "Archive", confirm=True,
                                      uidvalidity="1", confirmation=preview["confirmation"])

    def test_mark_uid_store_is_partial_and_stops_on_uidvalidity_change(self):
        mark = load("mark_email.py")
        imap = FakeIMAP({"INBOX": FakeMailbox(uidvalidity="1", messages=[
            FakeMessage("1", sample_message()), FakeMessage("2", sample_message())])},
                        failures={("store", "INBOX", "2"): True})
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=imap):
            result = mark.mark_emails("me@example.test", "token", ["1", "2"], "read", "INBOX", "1")
        self.assertEqual(result["status"], "partial")
        self.assertIn("\\Seen", imap.folders["INBOX"].messages[0].flags)
        self.assertTrue(all(call[0] != "STORE" for call in imap.log))
        self.assertEqual(result["failed"][0]["mail_id"], "2")

    def test_move_uidplus_fallback_stages_keep_unrelated_deleted_and_report_final_state(self):
        move = load("move_email.py")
        cases = (("copy", "source_unchanged"), ("store", "copied_destination_source_unchanged"),
                 ("expunge", "copied_destination_source_marked_deleted"), (None, "moved"))
        for stage, final_state in cases:
            with self.subTest(stage=stage):
                failures = {(stage, "INBOX", "9"): True} if stage else {}
                imap = FakeIMAP({"INBOX": FakeMailbox(uidvalidity="1", messages=[
                    FakeMessage("9", sample_message()), FakeMessage("10", sample_message(), flags={"\\Deleted"})]),
                    "Archive": FakeMailbox()}, capabilities=(b"uidplus",), failures=failures)
                result = self._preview_and_confirm(move, imap)
                self.assertEqual(result["results"][0]["final_state"], final_state)
                self.assertTrue(any(item.uid == "10" for item in imap.folders["INBOX"].messages))
                self.assertNotIn(("EXPUNGE",), imap.log)
                if stage:
                    self.assertEqual(result["status"], "error")
                else:
                    self.assertEqual(result["status"], "success")

    def test_move_capability_and_confirmation_fail_before_writes_and_move_failure_never_falls_back(self):
        move = load("move_email.py")
        imap = FakeIMAP({"INBOX": FakeMailbox(uidvalidity="1", messages=[FakeMessage("9", sample_message())]),
                         "Archive": FakeMailbox()}, capabilities=(b"MOVE", b"UIDPLUS"),
                        failures={("move", "INBOX", "9"): True})
        result = self._preview_and_confirm(move, imap)
        self.assertEqual(result["results"][0]["final_state"], "source_unchanged")
        self.assertFalse(any(call[:2] == ("UID", "COPY") for call in imap.log))
        self.assertEqual(imap.folders["Archive"].messages, [])

        unsupported = FakeIMAP({"INBOX": FakeMailbox(uidvalidity="1", messages=[FakeMessage("9", sample_message())]),
                                "Archive": FakeMailbox()}, capabilities=("IMAP4REV1",))
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=unsupported):
            preview = move.move_emails("me@example.test", "token", ["9"], "INBOX", "Archive", uidvalidity="1")
            blocked = move.move_emails("me@example.test", "token", ["9"], "INBOX", "Archive", confirm=True,
                                       uidvalidity="1", confirmation=preview["confirmation"])
        self.assertEqual(blocked["code"], "safe_move_unsupported")
        self.assertFalse(any(call[:2] == ("UID", "COPY") or call[:2] == ("UID", "MOVE") for call in unsupported.log))

        missing = FakeIMAP({"INBOX": FakeMailbox(uidvalidity="1", messages=[FakeMessage("9", sample_message())])},
                           capabilities=("UIDPLUS",))
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=missing):
            preview = move.move_emails("me@example.test", "token", ["9"], "INBOX", "Missing", uidvalidity="1")
            unavailable = move.move_emails("me@example.test", "token", ["9"], "INBOX", "Missing", confirm=True,
                                           uidvalidity="1", confirmation=preview["confirmation"])
        self.assertEqual(unavailable["code"], "destination_unavailable")
        self.assertFalse(any(call[:2] == ("UID", "COPY") for call in missing.log))


class M2FollowupContractTests(OfflineTestCase):
    class NoisyFetchIMAP(FakeIMAP):
        """Prepends an unsolicited literal for another UID to every UID FETCH."""
        def uid(self, command, *args):
            status, data = super().uid(command, *args)
            if command.upper() == "FETCH" and status == "OK":
                noise = (b'99 (UID 999 INTERNALDATE "01-Jan-2000 00:00:00 +0000" BODY[])',
                         sample_message(subject="UNSOLICITED"))
                return status, [noise, *data]
            return status, data

    def test_uid_fetch_selector_requires_exact_uid_and_supports_literal_first_metadata_last(self):
        response = select_uid_fetch([
            (b'5 (UID 5 BODY[])', b'wrong'),
            (b'right body', b'6 (UID 6 BODY[])'),
            b' INTERNALDATE "02-Jan-2024 10:00:00 +0000")',
        ], "6")
        self.assertIsNotNone(response)
        self.assertEqual(response.raw, b"right body")
        self.assertIn(b"INTERNALDATE", response.metadata)
        self.assertIsNone(select_uid_fetch([(b'5 (UID 5 BODY[])', b'wrong')], "6"))
        raw_with_uid = b"untrusted mail content: UID 9) is not protocol metadata"
        response = select_uid_fetch([(b'1 (UID 9 BODY[] {42}', raw_with_uid), b' INTERNALDATE "02-Jan-2024 10:00:00 +0000")'], "9")
        self.assertIsNotNone(response)
        self.assertEqual(response.raw, raw_with_uid)
        self.assertTrue(uid_fetch_exists([b"1 (UID 9)"], "9"))
        self.assertFalse(uid_fetch_exists([b"1 (UID 10)"], "9"))
        response = select_uid_fetch([
            (b'1 (BODY[] {4}', b'raw!'),
            b' UID 9 INTERNALDATE "02-Jan-2024 10:00:00 +0000")',
        ], "9")
        self.assertIsNotNone(response)
        self.assertEqual(response.raw, b"raw!")
        self.assertIn(b"UID 9", response.metadata)

    def test_all_uid_fetch_consumers_ignore_unsolicited_uid_and_accept_trailing_internaldate(self):
        get, download = load("get_email.py"), load("download_attachment.py")
        search, move, send = (load("search_emails.py"), load("move_email.py"), load("send_email.py"))
        message = FakeMessage("9", sample_message(subject="TARGET", attachment=("target.txt", b"x")),
                              internaldate="02-Jan-2024 10:00:00 +0000")
        imap = self.NoisyFetchIMAP({"INBOX": FakeMailbox(uidvalidity="1", messages=[message]),
                                    "Archive": FakeMailbox()})
        original_uid = imap.uid
        def trailing(command, *args):
            status, data = original_uid(command, *args)
            if command.upper() == "FETCH" and status == "OK":
                own = data[-1]
                if isinstance(own, tuple):
                    metadata, literal = own
                    data[-1:] = [(b'1 (BODY[] {4}', literal),
                                 b' UID 9 INTERNALDATE "02-Jan-2024 10:00:00 +0000")']
            return status, data
        imap.uid = trailing
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=imap):
            detail = get.get_emails("me@example.test", "token", ["9"], "INBOX", "1")
            self.assertEqual(detail["emails"][0]["subject"], "TARGET")
            with __import__("tempfile").TemporaryDirectory() as directory:
                attachment = download.download_attachments("me@example.test", "token", ["9"], "INBOX", "1", directory)
            self.assertEqual(attachment["success"][0]["downloaded"][0]["name"], "target.txt")
            listing = search.query_emails("me@example.test", "token")
            self.assertEqual(listing["emails"][0]["subject"], "TARGET")
            preview = move.move_emails("me@example.test", "token", ["9"], "INBOX", "Archive", uidvalidity="1")
            self.assertEqual(preview["emails"][0]["subject"], "TARGET")
            original = send.get_original_email("me@example.test", "token", "9", "INBOX", "1")
            self.assertEqual(original["subject"], "TARGET")

    def test_mark_verifies_existence_and_uses_flag_list(self):
        mark = load("mark_email.py")
        imap = FakeIMAP({"INBOX": FakeMailbox(uidvalidity="1", messages=[FakeMessage("1", sample_message())])},
                        failures={("uid_store_ok_empty", "INBOX", "404"): True})
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=imap):
            result = mark.mark_emails("me@example.test", "token", ["1", "404"], "read", "INBOX", "1")
        self.assertEqual(result["status"], "partial")
        stores = [call for call in imap.log if call[:2] == ("UID", "STORE")]
        self.assertEqual(stores[0][4], "(\\Seen)")
        self.assertFalse(any(call[2] == "404" and call[1] == "STORE" for call in imap.log if call[0] == "UID"))

    def test_move_dedupes_early_validation_partial_preview_and_refreshes_capabilities(self):
        move = load("move_email.py")
        imap = FakeIMAP({"INBOX": FakeMailbox(uidvalidity="1", messages=[FakeMessage("9", sample_message()),
                                                                           FakeMessage("10", sample_message())]),
                         "Archive": FakeMailbox()}, capabilities=("IMAP4REV1",),
                        capabilities_after_login=(b"move",))
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=imap):
            preview = move.move_emails("me@example.test", "token", ["9", "9"], "INBOX", "Archive", uidvalidity="1")
            self.assertEqual(len(preview["manifest"]["mailrefs"]), 1)
            confirmed = move.move_emails("me@example.test", "token", ["9", "9"], "INBOX", "Archive", confirm=True,
                                         uidvalidity="1", confirmation=preview["confirmation"])
        self.assertEqual(confirmed["status"], "success")
        self.assertEqual(confirmed["total"], 1)
        self.assertIn(("CAPABILITY",), imap.log)
        self.assertTrue(any(call[:2] == ("UID", "MOVE") for call in imap.log))

        partial = FakeIMAP({"INBOX": FakeMailbox(uidvalidity="1", messages=[FakeMessage("9", sample_message()),
                                                                               FakeMessage("10", sample_message())]),
                            "Archive": FakeMailbox()}, failures={("fetch", "INBOX", "10"): True})
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=partial):
            result = move.move_emails("me@example.test", "token", ["9", "10"], "INBOX", "Archive", uidvalidity="1")
        self.assertEqual(result["status"], "partial")

        stdout = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch.object(sys, "argv", ["move_email.py", "--mail_ids", "9", "--src_folder", "INBOX", "--uidvalidity", "1", "--dst_folder", "Archive", "--confirm"]), contextlib.redirect_stdout(stdout):
            self.assertEqual(move.main(), 1)
        self.assertEqual(json.loads(stdout.getvalue())["code"], "invalid_mailref")
        stdout = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch.object(sys, "argv", ["move_email.py", "--mail_ids", "9", "--src_folder", "INBOX", "--uidvalidity", "1", "--dst_folder", "Archive", "--delete"]), contextlib.redirect_stdout(stdout):
            self.assertEqual(move.main(), 1)
        self.assertEqual(json.loads(stdout.getvalue())["code"], "invalid_mailref")

        send = load("send_email.py")
        stdout = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch.object(sys, "argv", ["send_email.py", "--reply-to-id", "1:*", "--reply-folder", "INBOX", "--reply-uidvalidity", "1", "--body", "x"]), contextlib.redirect_stdout(stdout):
            self.assertEqual(send.main(), 1)
        self.assertEqual(len(stdout.getvalue().splitlines()), 1)
        self.assertEqual(json.loads(stdout.getvalue())["code"], "invalid_mailref")
        for reply_args in (("--reply-folder", "INBOX"), ("--reply-uidvalidity", "1"), ("--reply-quote",), ("--reply-to-id", "")):
            with self.subTest(reply_args=reply_args):
                stdout = io.StringIO()
                with patch.dict(os.environ, {}, clear=True), patch.object(sys, "argv", ["send_email.py", *reply_args, "--body", "x"]), contextlib.redirect_stdout(stdout):
                    self.assertEqual(send.main(), 1)
                lines = stdout.getvalue().splitlines()
                self.assertEqual(len(lines), 1)
                self.assertEqual(json.loads(lines[0])["code"], "invalid_mailref")

    def test_move_post_mutation_exceptions_are_never_reported_as_safe_retry_states(self):
        move = load("move_email.py")
        class PostMutationFake(FakeIMAP):
            def __init__(self, stage):
                super().__init__({"INBOX": FakeMailbox(uidvalidity="1", messages=[FakeMessage("9", sample_message())]),
                                  "Archive": FakeMailbox()}, capabilities=("UIDPLUS",))
                self.stage = stage
            def uid(self, command, *args):
                result = super().uid(command, *args)
                if command.upper() == self.stage:
                    raise RuntimeError("after mutation")
                return result
        expected = {"COPY": "indeterminate_after_copy_exception", "STORE": "copied_destination_source_state_unknown",
                    "EXPUNGE": "copied_destination_source_expunge_indeterminate"}
        for stage, final_state in expected.items():
            with self.subTest(stage=stage):
                imap = PostMutationFake(stage)
                result = self._confirm(move, imap)
                self.assertEqual(result["results"][0]["final_state"], final_state)
                if stage == "EXPUNGE":
                    # UID EXPUNGE completed before its response was lost.
                    self.assertEqual(imap.folders["INBOX"].messages, [])

        class MoveAfterMutation(PostMutationFake):
            def __init__(self):
                super().__init__("MOVE")
                self.capabilities = ("MOVE",)
        result = self._confirm(move, MoveAfterMutation())
        self.assertEqual(result["results"][0]["final_state"], "indeterminate_after_move_exception")

    def test_refreshed_capability_combinations_select_only_safe_path(self):
        move = load("move_email.py")
        cases = (("MOVE-only", ("MOVE",), "MOVE"), ("UIDPLUS-only", ("UIDPLUS",), "COPY"),
                 ("both", ("MOVE", "UIDPLUS"), "MOVE"), ("neither", (), None))
        for label, capabilities, expected in cases:
            with self.subTest(label=label):
                imap = FakeIMAP({"INBOX": FakeMailbox(uidvalidity="1", messages=[FakeMessage("9", sample_message())]),
                                 "Archive": FakeMailbox()}, capabilities=("IMAP4REV1",),
                                capabilities_after_login=capabilities)
                with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=imap):
                    preview = move.move_emails("me@example.test", "token", ["9"], "INBOX", "Archive", uidvalidity="1")
                    result = move.move_emails("me@example.test", "token", ["9"], "INBOX", "Archive", confirm=True,
                                              uidvalidity="1", confirmation=preview["confirmation"])
                calls = [call[1] for call in imap.log if call[0] == "UID"]
                if expected is None:
                    self.assertEqual(result["code"], "safe_move_unsupported")
                    self.assertNotIn("MOVE", calls)
                    self.assertNotIn("COPY", calls)
                else:
                    self.assertEqual(result["status"], "success")
                    self.assertIn(expected, calls)

    @staticmethod
    def _confirm(move, imap):
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=imap):
            preview = move.move_emails("me@example.test", "token", ["9"], "INBOX", "Archive", uidvalidity="1")
            return move.move_emails("me@example.test", "token", ["9"], "INBOX", "Archive", confirm=True,
                                    uidvalidity="1", confirmation=preview["confirmation"])

    def test_search_charset_from_precedence_datetime_since_and_pagination_total(self):
        search = load("search_emails.py")
        now = datetime(2024, 2, 1, 12, tzinfo=timezone.utc)
        messages = [FakeMessage(str(uid), sample_message(subject="snow", sender="雪"), internaldate=f"0{uid}-Feb-2024 11:00:00 +0000")
                    for uid in range(1, 4)]
        imap = FakeIMAP({"INBOX": FakeMailbox(uidvalidity="1", messages=messages)},
                        failures={("fetch", "INBOX", "2"): True})
        imap._matches = lambda _message, _criteria: True
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=imap):
            result = search.query_emails("me@example.test", "token", query="ignored", from_addr="雪",
                                         since="01-Feb-2024", recent="2h", limit=2, offset=1, now=now)
        search_call = next(call for call in imap.log if call[:2] == ("UID", "SEARCH"))
        self.assertEqual(search_call[2:4], ("CHARSET", "UTF-8"))
        self.assertIn('FROM "雪"', search_call)
        self.assertNotIn('SUBJECT "ignored"', search_call)
        self.assertIn("SINCE 01-Feb-2024", search_call)
        self.assertEqual(result["total_matched"], 3)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["failed"][0]["mail_id"], "2")

        ascii_imap = FakeIMAP({"INBOX": FakeMailbox(uidvalidity="1", messages=[FakeMessage("9", sample_message())])})
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=ascii_imap):
            ascii_result = search.query_emails("me@example.test", "token", query="meeting")
        self.assertEqual(ascii_result["status"], "success")
        ascii_call = next(call for call in ascii_imap.log if call[:2] == ("UID", "SEARCH"))
        self.assertIsNone(ascii_call[2])

        strict_imap = FakeIMAP({"INBOX": FakeMailbox(uidvalidity="1", messages=[FakeMessage("9", sample_message())])})
        strict_imap.select("INBOX")
        wrong_status, _ = strict_imap.uid("SEARCH", "UTF-8", 'SUBJECT "雪"')
        right_status, _ = strict_imap.uid("SEARCH", "CHARSET", "UTF-8", 'SUBJECT "雪"')
        self.assertEqual(wrong_status, "BAD")
        self.assertEqual(right_status, "OK")

    def test_recent_exact_filter_paginates_only_displayable_results(self):
        search = load("search_emails.py")
        imap = FakeIMAP({"INBOX": FakeMailbox(uidvalidity="1", messages=[
            FakeMessage("1", sample_message(), internaldate="03-Jan-2024 08:00:00 +0000"),
            FakeMessage("2", sample_message(), internaldate="03-Jan-2024 09:00:00 +0000"),
            FakeMessage("3", sample_message(), internaldate="03-Jan-2024 10:00:00 +0000"),
            FakeMessage("4", sample_message(), internaldate="03-Jan-2024 11:30:00 +0000"),
        ])}, failures={("fetch", "INBOX", "1"): True})
        now = datetime(2024, 1, 3, 12, tzinfo=timezone.utc)
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=imap):
            result = search.query_emails("me@example.test", "token", recent="1h", limit=15, now=now)
        self.assertEqual(result["total_matched"], 4)
        self.assertEqual(result["total_displayable"], 1)
        self.assertEqual(result["total"], 1)
        self.assertFalse(result["has_more"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["failed"][0]["mail_id"], "1")
