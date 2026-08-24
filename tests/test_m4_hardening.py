"""Offline acceptance tests for M4 bounded listing and local attachment safety."""
from __future__ import annotations

import contextlib
import errno
import hashlib
import importlib
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from email.parser import BytesParser
from unittest.mock import patch

from tests.support import BlockNetwork, FakeIMAP, FakeMailbox, FakeMessage, sample_message

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from qqmail_core.mailref import MailRef
from qqmail_core.mime import bodystructure_parts, decode_header_value, decode_transfer, extract_body_and_attachments, plain_text_part
from qqmail_core.imap_uid import (select_uid_fetch, select_uid_metadata,
                                  select_uid_section, uid_fetch_exists)


def load(name):
    # Attachment race/FD tests intentionally patch implementation globals.
    # After M5 ownership migration, exercise that authoritative module directly;
    # entrypoint compatibility is checked independently by M0/M5 tests.
    if name == "download_attachment.py":
        return importlib.import_module("qqmail_core.attachments")
    module_name = "m4_" + name.replace(".py", "")
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


def two_attachments(first=("same.txt", b"small"), second=("large.bin", b"0123456789")):
    message = EmailMessage()
    message["Subject"] = "=?made-up?Q?broken?="
    message["From"] = "sender@example.test"
    message.set_content("plain text body")
    for name, contents in (first, second):
        message.add_attachment(contents, maintype="application", subtype="octet-stream", filename=name)
    return message.as_bytes()


class BoundedListTests(OfflineTestCase):
    def test_late_header_failure_makes_empty_page_error_after_metadata_fetch(self):
        search = load("search_emails.py")
        class HeaderFailIMAP(FakeIMAP):
            def uid(self, command, *args):
                if command.upper() == "FETCH" and len(args) > 1 and "HEADER.FIELDS" in str(args[1]):
                    self.log.append(("UID", "FETCH") + args)
                    return "NO", [b"late header failure"]
                return super().uid(command, *args)
        imap = HeaderFailIMAP({"INBOX": FakeMailbox(messages=[FakeMessage("1", sample_message())])})
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=imap):
            result = search.query_emails("me@example.test", "token", limit=1)
        self.assertEqual((result["status"], result["total"], result["emails"]), ("error", 0, []))
        self.assertEqual(result["total_displayable"], 1)
        fetches = [call[3] for call in imap.log if call[:2] == ("UID", "FETCH")]
        self.assertIn("(UID INTERNALDATE)", fetches)
        self.assertTrue(any("HEADER.FIELDS" in query for query in fetches))

    def test_fake_fetch_failure_sequence_is_consumed_once_per_fetch(self):
        imap = FakeIMAP({"INBOX": FakeMailbox(messages=[FakeMessage("1", sample_message())])},
                        failures={"fetch": [False, True]})
        imap.select("INBOX", readonly=True)
        self.assertEqual(imap.uid("FETCH", "1", "(UID INTERNALDATE)")[0], "OK")
        self.assertEqual(imap.uid("FETCH", "1", "(UID INTERNALDATE)")[0], "NO")
    def test_section_selector_rejects_wrong_section_and_oversize_literal(self):
        wrong = [(b"1 (UID 9 BODY[2]<0>)", b"attachment")]
        self.assertIsNone(select_uid_section(wrong, "9", "1", maximum=10))
        too_large = [(b"1 (UID 9 BODY[1]<0>)", b"abcdef")]
        self.assertIsNone(select_uid_section(too_large, "9", "1", maximum=5))
        interleaved = [(b"1 (UID 9 BODY[2]<0>)", b"ATTACHMENT"), b"2 (UID 10 BODY[1]<0>)"]
        self.assertIsNone(select_uid_section(interleaved, "9", "1", maximum=100))
        numbered_literal = [(b"1 (UID 9 BODY[1]<0>)", b"1 (hello)")]
        self.assertEqual(select_uid_section(numbered_literal, "9", "1").raw, b"1 (hello)")
        marker_in_literal = [(b"1 (UID 9 BODY[1]<0>)", b"prefix BODY[1]<0> suffix")]
        self.assertEqual(select_uid_section(marker_in_literal, "9", "1").raw, b"prefix BODY[1]<0> suffix")
        fetch_looking_body = b"1 (hello)"
        self.assertEqual(select_uid_section([(b"1 (UID 9 BODY[1]<0>)", fetch_looking_body)], "9", "1",
                                            expected_length=len(fetch_looking_body)).raw, fetch_looking_body)
        literal_first = [(b"1 (BODY[1]<0>", b"hello"), b" UID 9)"]
        self.assertEqual(select_uid_section(literal_first, "9", "1").raw, b"hello")
        self.assertIsNone(select_uid_metadata([(b"1 (UID 9 FLAGS (\\Seen))", b"literal")], "9", required_item="BODYSTRUCTURE"))
        self.assertIsNone(select_uid_metadata([b"1 (UID 9 XBODYSTRUCTURE NIL)"], "9", required_item="BODYSTRUCTURE"))
        self.assertIsNone(select_uid_metadata([b'1 (UID 9 NOTINTERNALDATE "x")'], "9", required_item="INTERNALDATE"))
        self.assertIsNotNone(select_uid_metadata([b"1 (UID 9 BODYSTRUCTURE (NIL))"], "9", required_item="BODYSTRUCTURE"))

    def test_metadata_items_ignore_same_uid_flags_spoofs_end_to_end(self):
        class SpoofingIMAP(FakeIMAP):
            def uid(self, command, *args):
                status, data = super().uid(command, *args)
                if command.upper() != "FETCH" or status != "OK" or len(args) < 2:
                    return status, data
                query = str(args[1])
                if query == "(UID INTERNALDATE)":
                    return "OK", [b"1 (UID 9 FLAGS (INTERNALDATE))", *data]
                if query == "(UID BODYSTRUCTURE)":
                    return "OK", [b"1 (UID 9 FLAGS (BODYSTRUCTURE))", *data]
                return status, data

        raw = sample_message(attachment=("real.bin", b"payload"))
        self.assertEqual(select_uid_metadata([
            b"1 (UID 9 FLAGS (BODYSTRUCTURE))", b"1 (UID 9 BODYSTRUCTURE (NIL))"], "9",
            required_item="BODYSTRUCTURE"), b"1 (UID 9 BODYSTRUCTURE (NIL))")
        search = load("search_emails.py")
        imap = SpoofingIMAP({"INBOX": FakeMailbox(messages=[FakeMessage("9", raw)])})
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=imap):
            listed = search.query_emails("me@example.test", "token", limit=1)
        self.assertEqual((listed["status"], listed["total"]), ("success", 1))
        download = load("download_attachment.py")
        with tempfile.TemporaryDirectory() as directory:
            imap = SpoofingIMAP({"INBOX": FakeMailbox(messages=[FakeMessage("9", raw)])})
            downloaded = download.download_attachments_for_mail(imap, MailRef("INBOX", "1", "9"), directory)
            self.assertEqual((downloaded["status"], downloaded["downloaded"][0]["name"]), ("success", "real.bin"))

    def test_uid_selectors_ignore_quoted_or_nested_uids_and_accept_top_level_trailer(self):
        metadata = (b'1 (BODYSTRUCTURE ("APPLICATION" "OCTET-STREAM" ("NAME" "(UID 9)") '
                    b'NIL NIL "7BIT" 6 NIL NIL NIL NIL) BODY[1]<0> {6}')
        response = [(metadata, b'secret'), b' UID 10)']
        self.assertIsNone(select_uid_section(response, "9", "1", expected_length=6))
        self.assertEqual(select_uid_section(response, "10", "1", expected_length=6).raw, b"secret")
        self.assertIsNone(select_uid_fetch(response, "9"))
        full_response = [(b'1 (BODYSTRUCTURE ("TEXT" "PLAIN" ("NAME" "(UID 9)") NIL NIL '
                          b'"7BIT" 6 1 NIL NIL NIL) BODY[] {6}', b'secret'), b' UID 10)']
        self.assertEqual(select_uid_fetch(full_response, "10").raw, b"secret")
        nested_only = b'1 (FLAGS ((UID 9)) INTERNALDATE "02-Jan-2024 10:00:00 +0000")'
        self.assertIsNone(select_uid_metadata([nested_only], "9", required_item="INTERNALDATE"))
        top_level_trailer = nested_only[:-1] + b" UID 10)"
        self.assertIsNotNone(select_uid_metadata([top_level_trailer], "10", required_item="INTERNALDATE"))
        self.assertFalse(uid_fetch_exists([nested_only], "9"))
        self.assertTrue(uid_fetch_exists([top_level_trailer], "10"))

    def test_uid_fetch_continuations_stop_at_independent_fetch_and_section_is_top_level_only(self):
        borrowed_uid = [(b"1 (BODY[] {5}", b"wrong"), b")", b"2 (UID 9)"]
        self.assertIsNone(select_uid_fetch(borrowed_uid, "9"))
        owned_uid = [(b"1 (BODY[] {5}", b"right"), b" UID 9)"]
        self.assertEqual(select_uid_fetch(owned_uid, "9").raw, b"right")

        nested_marker = [(
            b'1 (UID 9 BODYSTRUCTURE ("TEXT" "PLAIN" ("NAME" "BODY[1]<0> ") '
            b'NIL NIL "7BIT" 6 1 NIL NIL NIL) BODY[2]<0> {6}', b"secret"), b")"]
        self.assertIsNone(select_uid_section(nested_marker, "9", "1", expected_length=6))
        self.assertEqual(select_uid_section(nested_marker, "9", "2", expected_length=6).raw, b"secret")

    def test_literal_continuations_preserve_their_actual_separator_boundary(self):
        full_closed = (b"1 (UID 9 BODY[] {5}", b"hello")
        section_closed = (b"1 (UID 9 BODY[1]<0> {5}", b"hello")
        self.assertEqual(select_uid_fetch([full_closed, b")"], "9").raw, b"hello")
        self.assertEqual(select_uid_section([section_closed, b")"], "9", "1", expected_length=5).raw,
                         b"hello")
        full_prefix = (b"1 (BODY[] {5}", b"hello")
        section_prefix = (b"1 (BODY[1]<0> {5}", b"hello")
        self.assertEqual(select_uid_fetch([full_prefix, b" UID 9)"], "9").raw, b"hello")
        self.assertEqual(select_uid_section([section_prefix, b" UID 9)"], "9", "1", expected_length=5).raw,
                         b"hello")
        self.assertIsNone(select_uid_fetch([full_prefix, b"UID 9)"], "9"))
        self.assertIsNone(select_uid_section([section_prefix, b"UID 9)"], "9", "1", expected_length=5))

    def test_literal_prefix_must_end_exactly_at_marker_or_legacy_body_boundary(self):
        full_marker_space = (b"1 (BODY[] {5} ", b"hello")
        section_marker_space = (b"1 (BODY[1]<0> {5} ", b"hello")
        self.assertIsNone(select_uid_fetch([full_marker_space, b"UID 9)"], "9"))
        self.assertIsNone(select_uid_section([section_marker_space, b"UID 9)"], "9", "1", expected_length=5))
        self.assertIsNone(select_uid_fetch([(b"1 (UID 9 BODY[] {5} ", b"hello"), b")"], "9"))
        self.assertIsNone(select_uid_section([(
            b"1 (UID 9 BODY[1]<0> {5} ", b"hello"), b")"], "9", "1", expected_length=5))

        full_legacy_space = (b"1 (BODY[] ", b"hello")
        section_legacy_space = (b"1 (BODY[1]<0> ", b"hello")
        self.assertIsNone(select_uid_fetch([full_legacy_space, b"UID 9)"], "9"))
        self.assertIsNone(select_uid_section([section_legacy_space, b"UID 9)"], "9", "1", expected_length=5))

    def test_fetch_envelope_is_single_balanced_root_and_section_binds_its_literal_marker(self):
        self.assertIsNone(select_uid_fetch([(b"1 (BODY[]) 2 (UID 9)", b"secret")], "9"))
        self.assertIsNone(select_uid_metadata([
            b'1 (INTERNALDATE "02-Jan-2024 00:00:00 +0000") 2 (UID 9)'], "9",
            required_item="INTERNALDATE"))
        for malformed in (b"1 (UID nope UID 9)", b"1 (UID 9", b"1 (UID 9))"):
            self.assertIsNone(select_uid_metadata([malformed], "9"))

        multiple_sections = [(b"1 (UID 9 BODY[1]<0> NIL BODY[2]<0> {6}", b"secret"), b")"]
        self.assertIsNone(select_uid_section(multiple_sections, "9", "1", expected_length=6))
        self.assertEqual(select_uid_section(multiple_sections, "9", "2", expected_length=6).raw, b"secret")
        self.assertIsNone(select_uid_section([(b"1 (UID 9 BODY[2]<0> {7}", b"secret"), b")"],
                                              "9", "2", expected_length=6))
        header = [(b"1 (UID 9 BODY[HEADER.FIELDS (SUBJECT FROM DATE)]<0> {4}", b"X: y"), b")"]
        self.assertEqual(select_uid_section(header, "9", "HEADER.FIELDS (SUBJECT FROM DATE)",
                                            expected_length=4).raw, b"X: y")
        trailing_uid = [(b"1 (BODY[1]<0> {6}", b"secret"), b" UID 9)"]
        self.assertEqual(select_uid_section(trailing_uid, "9", "1", expected_length=6).raw, b"secret")

    def test_full_message_selector_and_uid_exists_bind_only_literal_free_protocol_items(self):
        self.assertIsNone(select_uid_fetch([(b"1 (UID 9 BODY[2]<0>)", b"section")], "9"))
        self.assertIsNone(select_uid_fetch([(b"1 (UID 9 FLAGS (\\Seen))", b"arbitrary")], "9"))
        self.assertIsNone(select_uid_fetch([(b"1 (UID 9 BODY[] {5}", b"wrong", b"right"), b")"], "9"))
        quoted_only = [(b'1 (UID 9 BODYSTRUCTURE ("TEXT" "PLAIN" ("NAME" "BODY[]") NIL NIL '
                        b'"7BIT" 5 1 NIL NIL NIL) {5}', b"wrong"), b")"]
        self.assertIsNone(select_uid_fetch(quoted_only, "9"))
        full = [(b"1 (BODY.PEEK[]<0> {5}", b"hello"), b" UID 9)"]
        self.assertEqual(select_uid_fetch(full, "9").raw, b"hello")
        literal_first = [(b"hello", b"1 (BODY[] {5}"), b" UID 9)"]
        self.assertEqual(select_uid_fetch(literal_first, "9").raw, b"hello")

        self.assertTrue(uid_fetch_exists([b"1 (UID 9)"], "9"))
        self.assertTrue(uid_fetch_exists([(b"1 (UID 9)",)], "9"))
        literal_spoof = [(b"1 (UID 10 BODY[] {9}", b"2 (UID 9)"), b")"]
        self.assertFalse(uid_fetch_exists(literal_spoof, "9"))
        self.assertFalse(uid_fetch_exists([(b"1 (UID 9",), b")"], "9"))

    def test_fetch_looking_tuple_ends_require_one_valid_literal_direction(self):
        # Both orientations are syntactically valid BODY[] FETCH prefixes, so
        # their roles are unknowable and must not be chosen by tuple position.
        first, second = b"1 (BODY[] {14}", b"2 (BODY[] {14}"
        self.assertIsNone(select_uid_fetch([(first, second), b" UID 9)"], "9"))
        section_first, section_second = b"1 (BODY[1]<0> {18}", b"2 (BODY[1]<0> {18}"
        self.assertIsNone(select_uid_section([(section_first, section_second), b" UID 9)"], "9", "1",
                                              expected_length=18))

        fetch_looking = b"1 (hello)"
        metadata_first = [(f"1 (BODY[] {{{len(fetch_looking)}}}".encode(), fetch_looking), b" UID 9)"]
        metadata_last = [(fetch_looking, f"2 (BODY[] {{{len(fetch_looking)}}}".encode()), b" UID 9)"]
        self.assertEqual(select_uid_fetch(metadata_first, "9").raw, fetch_looking)
        self.assertEqual(select_uid_fetch(metadata_last, "9").raw, fetch_looking)
        section_first_ok = [(f"1 (BODY[1]<0> {{{len(fetch_looking)}}}".encode(), fetch_looking), b" UID 9)"]
        section_last_ok = [(fetch_looking, f"2 (BODY[1]<0> {{{len(fetch_looking)}}}".encode()), b" UID 9)"]
        self.assertEqual(select_uid_section(section_first_ok, "9", "1", expected_length=len(fetch_looking)).raw,
                         fetch_looking)
        self.assertEqual(select_uid_section(section_last_ok, "9", "1", expected_length=len(fetch_looking)).raw,
                         fetch_looking)
        duplicate_tuples = [(b"1 (BODY[] {5}", b"hello"), b" UID 9)",
                            (b"2 (BODY[] {5}", b"world"), b" UID 9)"]
        self.assertIsNone(select_uid_fetch(duplicate_tuples, "9"))

    def test_fetch_lexer_does_not_promote_bracket_brace_or_angle_contents_to_items(self):
        self.assertIsNone(select_uid_fetch([(b"1 (UID 9 X {BODY[] {5}", b"hello"), b")"], "9"))
        self.assertIsNone(select_uid_section([(b"1 (UID 9 X <BODY[1]<0> {5}", b"hello"), b")"],
                                              "9", "1", expected_length=5))
        self.assertIsNone(select_uid_metadata([
            b'1 (UID 9 BODY[INTERNALDATE "02-Jan-2024 00:00:00 +0000" ] NIL)'], "9",
            required_item="INTERNALDATE"))
        self.assertIsNone(select_uid_metadata([b"1 (UID 9 X {BODYSTRUCTURE (NIL) })"], "9",
                                              required_item="BODYSTRUCTURE"))
        for buried_uid in (b"BODY[UID 9 ]", b"X {UID 9 }", b"X <UID 9 >"):
            metadata = b"1 (" + buried_uid + b")"
            self.assertIsNone(select_uid_metadata([metadata], "9"))
            self.assertFalse(uid_fetch_exists([metadata], "9"))
            response = [(b"1 (" + buried_uid + b" BODY[] {5}", b"hello"), b")"]
            self.assertIsNone(select_uid_fetch(response, "9"))
            section = [(b"1 (" + buried_uid + b" BODY[1]<0> {5}", b"hello"), b")"]
            self.assertIsNone(select_uid_section(section, "9", "1", expected_length=5))

        header = [(b"1 (BODY[HEADER.FIELDS (SUBJECT FROM DATE)]<0> {4}", b"X: y"), b" UID 9)"]
        self.assertEqual(select_uid_section(header, "9", "HEADER.FIELDS (SUBJECT FROM DATE)",
                                            expected_length=4).raw, b"X: y")
        full = [(b"1 (BODY[]<0> {5}", b"hello"), b" UID 9)"]
        self.assertEqual(select_uid_fetch(full, "9").raw, b"hello")

    def test_literal_item_must_finish_in_its_tuple_prefix_and_be_unique(self):
        # Continuations may close a literal-bearing FETCH and supply its UID,
        # but must not manufacture the current literal's BODY item or marker.
        self.assertIsNone(select_uid_fetch([(b"1 (", b"hello"), b" BODY[] {5} UID 9)"], "9"))
        self.assertIsNone(select_uid_fetch([(b"1 (BODY[]", b"hello"), b" {5} UID 9)"], "9"))
        self.assertIsNone(select_uid_section([(b"1 (", b"hello"), b" BODY[1]<0> {5} UID 9)"],
                                              "9", "1", expected_length=5))
        self.assertIsNone(select_uid_section([(b"1 (BODY[1]<0>", b"hello"), b" {5} UID 9)"],
                                              "9", "1", expected_length=5))

        self.assertIsNone(select_uid_fetch([(
            b"1 (UID 9 BODY[2] {5} BODY[] {5}", b"hello"), b")"], "9"))
        self.assertIsNone(select_uid_section([(
            b"1 (UID 9 X {5} BODY[1]<0> {5}", b"hello"), b")"],
                                              "9", "1", expected_length=5))
        metadata = b'1 (UID 9 X {5} INTERNALDATE "02-Jan-2024 00:00:00 +0000")'
        self.assertIsNone(select_uid_metadata([metadata], "9", required_item="INTERNALDATE"))
        self.assertIsNone(select_uid_metadata([b"1 (UID 9 X {5})"], "9"))
        self.assertFalse(uid_fetch_exists([b"1 (UID 9 X {5})"], "9"))

        # Legacy FakeIMAP-style rows without an explicit marker remain safe
        # only where the sole target BODY item ends exactly at the tuple prefix.
        legacy = [(b"1 (BODY[]", b"hello"), b" UID 9)"]
        self.assertEqual(select_uid_fetch(legacy, "9").raw, b"hello")

    def test_oversized_literal_and_partial_numbers_fail_closed_without_raising(self):
        too_wide = b"9" * 5000
        self.assertIsNone(select_uid_fetch([(
            b"1 (UID 9 BODY[] {" + too_wide + b"}", b"hello"), b")"], "9"))
        self.assertIsNone(select_uid_fetch([(
            b"1 (UID 9 BODY[]<" + too_wide + b"> {5}", b"hello"), b")"], "9"))
        self.assertIsNone(select_uid_section([(
            b"1 (UID 9 BODY[1]<" + too_wide + b"> {5}", b"hello"), b")"],
                                              "9", "1", expected_length=5))
        self.assertIsNone(select_uid_section([(
            b"1 (UID 9 BODY[1]<0> {" + too_wide + b"}", b"hello"), b")"],
                                              "9", "1", expected_length=5))
        self.assertIsNone(select_uid_metadata([
            b"1 (UID 9 X {" + too_wide + b"} INTERNALDATE \"02-Jan-2024 00:00:00 +0000\")"],
            "9", required_item="INTERNALDATE"))
        self.assertIsNone(select_uid_metadata([
            b"1 (UID 9 BODY[]<" + too_wide + b"> {5} INTERNALDATE \"02-Jan-2024 00:00:00 +0000\")"],
            "9", required_item="INTERNALDATE"))
        self.assertFalse(uid_fetch_exists([
            b"1 (UID 9 BODY[]<" + too_wide + b"> {5})"], "9"))
        self.assertFalse(uid_fetch_exists([
            b"1 (UID 9 X {" + too_wide + b"})"], "9"))

    def test_fetch_lexer_accepts_bounded_numbers_and_nonextended_body_lists(self):
        internaldate = b'"02-Jan-2024 00:00:00 +0000"'
        metadata = b"1 (UID 9 RFC822.SIZE 123 INTERNALDATE " + internaldate + b")"
        self.assertIsNotNone(select_uid_metadata([metadata], "9", required_item="INTERNALDATE"))
        full = [(b"1 (UID 9 RFC822.SIZE 123 BODY[] {5}", b"hello"), b")"]
        self.assertEqual(select_uid_fetch(full, "9").raw, b"hello")
        section = [(b"1 (UID 9 RFC822.SIZE 123 BODY[1]<0> {5}", b"hello"), b")"]
        self.assertEqual(select_uid_section(section, "9", "1", expected_length=5).raw, b"hello")

        body_list = b'BODY ("TEXT" "PLAIN" NIL NIL NIL "7BIT" 5 1)'
        metadata = b"1 (UID 9 " + body_list + b" INTERNALDATE " + internaldate + b")"
        self.assertIsNotNone(select_uid_metadata([metadata], "9", required_item="INTERNALDATE"))
        full = [(b"1 (UID 9 " + body_list + b" BODY[] {5}", b"hello"), b")"]
        self.assertEqual(select_uid_fetch(full, "9").raw, b"hello")
        section = [(b"1 (UID 9 " + body_list + b" BODY[1]<0> {5}", b"hello"), b")"]
        self.assertEqual(select_uid_section(section, "9", "1", expected_length=5).raw, b"hello")

        self.assertIsNone(select_uid_metadata([
            b"1 (UID 9 RFC822.SIZE 123x INTERNALDATE " + internaldate + b")"], "9",
            required_item="INTERNALDATE"))
        self.assertIsNone(select_uid_metadata([
            b"1 (UID 9 RFC822.SIZE " + b"9" * 5000 + b" INTERNALDATE " + internaldate + b")"],
            "9", required_item="INTERNALDATE"))
        self.assertIsNone(select_uid_fetch([(
            b"1 (UID 9 BODY (NIL BODY[] {5}", b"hello"), b")"], "9"))
        self.assertIsNone(select_uid_metadata([
            b"1 (UID 9 BODY.PEEK (NIL) INTERNALDATE " + internaldate + b")"], "9",
            required_item="INTERNALDATE"))

    def test_typed_sizes_flat_body_values_and_32bit_protocol_numbers(self):
        date = b'"02-Jan-2024 00:00:00 +0000"'
        self.assertIsNotNone(select_uid_metadata([
            b"1 (UID 9 RFC822.SIZE 123 INTERNALDATE " + date + b")"], "9",
            required_item="INTERNALDATE"))
        for invalid_size in (b"NIL", b'"123"', b"4294967296"):
            self.assertIsNone(select_uid_metadata([
                b"1 (UID 9 RFC822.SIZE " + invalid_size + b" INTERNALDATE " + date + b")"], "9",
                required_item="INTERNALDATE"))
        self.assertFalse(uid_fetch_exists([b"1 (UID 9 RFC822.SIZE NIL)"], "9"))
        self.assertIsNone(select_uid_fetch([(
            b"1 (UID 9 RFC822.SIZE NIL BODY[] {5}", b"hello"), b")"], "9"))
        self.assertIsNone(select_uid_section([(
            b"1 (UID 9 RFC822.SIZE \"123\" BODY[1]<0> {5}", b"hello"), b")"], "9", "1",
                                              expected_length=5))
        self.assertIsNotNone(select_uid_metadata([
            b"1 (UID 9 RFC822.SIZE 0 INTERNALDATE " + date + b")"], "9",
            required_item="INTERNALDATE"))

        self.assertIsNone(select_uid_metadata([b"1 (UID 9 BODY[1])"], "9"))
        self.assertFalse(uid_fetch_exists([b"1 (UID 9 BODY[1])"], "9"))
        literal_free = b"1 (UID 9 BODY[1] NIL)"
        self.assertEqual(select_uid_metadata([literal_free], "9"), literal_free)
        self.assertTrue(uid_fetch_exists([literal_free], "9"))

        self.assertIsNone(select_uid_fetch([(b"1 (UID 9 BODY[] {5})", b"hello")], "9"))
        self.assertEqual(select_uid_fetch([(b"1 (UID 9 BODY[] {5}", b"hello"), b")"], "9").raw, b"hello")

        maximum = b"4294967295"
        self.assertIsNotNone(select_uid_metadata([
            b"1 (UID " + maximum + b" INTERNALDATE " + date + b")"], maximum.decode(),
            required_item="INTERNALDATE"))
        self.assertTrue(uid_fetch_exists([b"1 (UID " + maximum + b")"], maximum.decode()))
        self.assertEqual(select_uid_fetch([(
            b"1 (UID " + maximum + b" BODY[] {5}", b"hello"), b")"], maximum.decode()).raw, b"hello")
        self.assertEqual(select_uid_section([(
            b"1 (UID " + maximum + b" BODY[1]<0> {5}", b"hello"), b")"], maximum.decode(), "1",
                                              expected_length=5).raw, b"hello")
        for invalid_uid in (b"0", b"09", b"0000000009", b"4294967296", b"9" * 5000):
            uid = invalid_uid.decode()
            self.assertIsNone(select_uid_metadata([
                b"1 (UID " + invalid_uid + b" INTERNALDATE " + date + b")"], uid,
                required_item="INTERNALDATE"))
            self.assertFalse(uid_fetch_exists([b"1 (UID " + invalid_uid + b")"], uid))
            self.assertIsNone(select_uid_fetch([(
                b"1 (UID " + invalid_uid + b" BODY[] {5}", b"hello"), b")"], uid))
            self.assertIsNone(select_uid_section([(
                b"1 (UID " + invalid_uid + b" BODY[1]<0> {5}", b"hello"), b")"], uid,
                                                  "1", expected_length=5))

        self.assertEqual(select_uid_fetch([(b"1 (UID 9 BODY[] {0}", b""), b")"], "9").raw, b"")
        self.assertEqual(select_uid_section([(b"1 (UID 9 BODY[1]<0> {0}", b""), b")"], "9", "1",
                                            expected_length=0).raw, b"")
        self.assertIsNone(select_uid_fetch([(
            b"1 (UID 9 BODY[] {4294967296}", b"hello"), b")"], "9"))
        self.assertIsNone(select_uid_section([(
            b"1 (UID 9 BODY[1]<4294967296> {5}", b"hello"), b")"], "9", "1",
                                              expected_length=5))

    def test_fetch_root_sequence_is_a_strict_32bit_nz_number(self):
        date = b'"02-Jan-2024 00:00:00 +0000"'
        for sequence in (b"0", b"01", b"4294967296", b"9" * 5000):
            self.assertIsNone(select_uid_metadata([
                sequence + b" (UID 9 INTERNALDATE " + date + b")"], "9",
                required_item="INTERNALDATE"))
            self.assertFalse(uid_fetch_exists([sequence + b" (UID 9)"], "9"))
            self.assertIsNone(select_uid_fetch([(
                sequence + b" (UID 9 BODY[] {5}", b"hello"), b")"], "9"))
            self.assertIsNone(select_uid_section([(
                sequence + b" (UID 9 BODY[1]<0> {5}", b"hello"), b")"], "9", "1",
                                                  expected_length=5))

        maximum = b"4294967295"
        self.assertIsNotNone(select_uid_metadata([
            maximum + b" (UID 9 INTERNALDATE " + date + b")"], "9", required_item="INTERNALDATE"))
        self.assertTrue(uid_fetch_exists([maximum + b" (UID 9)"], "9"))
        self.assertEqual(select_uid_fetch([(
            maximum + b" (UID 9 BODY[] {5}", b"hello"), b")"], "9").raw, b"hello")
        self.assertEqual(select_uid_section([(
            maximum + b" (UID 9 BODY[1]<0> {5}", b"hello"), b")"], "9", "1",
                                            expected_length=5).raw, b"hello")

    def test_known_fetch_item_types_spacing_and_complete_body_values(self):
        date = b'"02-Jan-2024 00:00:00 +0000"'
        valid_metadata = b"1 (UID 9 ENVELOPE (NIL) RFC822 NIL INTERNALDATE " + date + b")"
        self.assertIsNotNone(select_uid_metadata([valid_metadata], "9", required_item="INTERNALDATE"))
        valid_full = [(b"1 (UID 9 ENVELOPE (NIL) RFC822 NIL BODY[] {5}", b"hello"), b")"]
        self.assertEqual(select_uid_fetch(valid_full, "9").raw, b"hello")
        valid_section = [(b"1 (UID 9 ENVELOPE (NIL) RFC822 NIL BODY[1]<0> {5}", b"hello"), b")"]
        self.assertEqual(select_uid_section(valid_section, "9", "1", expected_length=5).raw, b"hello")

        for bad_value in (b"ENVELOPE NIL", b"ENVELOPE 1", b"RFC822 123", b"RFC822 (NIL)"):
            self.assertIsNone(select_uid_metadata([
                b"1 (UID 9 " + bad_value + b" INTERNALDATE " + date + b")"], "9",
                required_item="INTERNALDATE"))
            self.assertFalse(uid_fetch_exists([b"1 (UID 9 " + bad_value + b")"], "9"))
        self.assertIsNone(select_uid_metadata([
            b"1 (UID 9 INTERNALDATE" + date + b")"], "9", required_item="INTERNALDATE"))
        self.assertIsNone(select_uid_metadata([
            b"1 (UID 9 BODYSTRUCTURE(NIL))"], "9", required_item="BODYSTRUCTURE"))
        self.assertIsNone(select_uid_fetch([(b"1 (UID 9 BODY[]{5}", b"hello"), b")"], "9"))
        self.assertIsNone(select_uid_section([(
            b"1 (UID 9 BODY[1]<0>{5}", b"hello"), b")"], "9", "1", expected_length=5))

        self.assertIsNone(select_uid_fetch([(
            b"1 (UID 9 BODY[] {5} BODY[1]", b"hello"), b")"], "9"))
        self.assertIsNone(select_uid_section([(
            b"1 (UID 9 BODY[1]<0> {5} BODY[2]", b"hello"), b")"], "9", "1", expected_length=5))
        # A distinct, explicit NIL section remains structurally complete.
        self.assertEqual(select_uid_fetch([(
            b"1 (UID 9 BODY[2] NIL BODY[] {5}", b"hello"), b")"], "9").raw, b"hello")

    def test_metadata_selector_requires_one_global_matching_row(self):
        first_date = b'"02-Jan-2024 00:00:00 +0000"'
        second_date = b'"03-Jan-2024 00:00:00 +0000"'
        self.assertIsNone(select_uid_metadata([
            b"1 (UID 9 INTERNALDATE " + first_date + b")",
            b"2 (UID 9 INTERNALDATE " + second_date + b")"], "9", required_item="INTERNALDATE"))
        self.assertIsNone(select_uid_metadata([
            b"1 (UID 9 BODYSTRUCTURE (NIL))",
            b"2 (UID 9 BODYSTRUCTURE (\"TEXT\" \"PLAIN\" NIL NIL NIL \"7BIT\" 1 1))"], "9",
            required_item="BODYSTRUCTURE"))
        only = b"1 (UID 9 INTERNALDATE " + first_date + b")"
        self.assertEqual(select_uid_metadata([only], "9", required_item="INTERNALDATE"), only)
        self.assertTrue(uid_fetch_exists([
            b"1 (UID 9)", b"2 (UID 9)"], "9"))

    def test_generic_extension_numbers_allow_bounded_uint64_not_protocol_uint32(self):
        date = b'"02-Jan-2024 00:00:00 +0000"'
        gmail_id = b"1278455344230334865"
        extensions = b"X-GM-MSGID " + gmail_id + b" X-GM-THRID " + gmail_id + b" MODSEQ (123)"
        self.assertIsNotNone(select_uid_metadata([
            b"1 (UID 9 " + extensions + b" INTERNALDATE " + date + b")"], "9",
            required_item="INTERNALDATE"))
        self.assertEqual(select_uid_fetch([(
            b"1 (UID 9 " + extensions + b" BODY[] {5}", b"hello"), b")"], "9").raw, b"hello")
        self.assertEqual(select_uid_section([(
            b"1 (UID 9 " + extensions + b" BODY[1]<0> {5}", b"hello"), b")"], "9", "1",
                                            expected_length=5).raw, b"hello")
        for invalid in (b"18446744073709551616", b"9" * 5000):
            self.assertIsNone(select_uid_metadata([
                b"1 (UID 9 X-GM-MSGID " + invalid + b" INTERNALDATE " + date + b")"], "9",
                required_item="INTERNALDATE"))

    def test_fetch_top_level_items_require_sp_between_values(self):
        date = b'"02-Jan-2024 00:00:00 +0000"'
        self.assertTrue(uid_fetch_exists([b"1 (FLAGS () UID 9)"], "9"))
        self.assertTrue(uid_fetch_exists([b"1 (BODYSTRUCTURE (NIL) UID 9)"], "9"))
        self.assertIsNotNone(select_uid_metadata([
            b"1 (INTERNALDATE " + date + b" UID 9)"], "9", required_item="INTERNALDATE"))
        self.assertTrue(uid_fetch_exists([b'1 (RFC822 "short" UID 9)'], "9"))
        self.assertTrue(uid_fetch_exists([b"1 (ENVELOPE (NIL) UID 9)"], "9"))
        self.assertTrue(uid_fetch_exists([b"1 (RFC822.SIZE 1 UID 9)"], "9"))
        self.assertTrue(uid_fetch_exists([b'1 (X-GM-LABELS ("one") UID 9)'], "9"))

        for malformed in (
            b"1 (FLAGS ()UID 9)",
            b"1 (BODYSTRUCTURE (NIL)UID 9)",
            b"1 (INTERNALDATE \"02-Jan-2024 00:00:00 +0000\"UID 9)",
            b'1 (RFC822 "short"UID 9)',
            b"1 (ENVELOPE (NIL)UID 9)",
            b"1 (RFC822.SIZE 1UID 9)",
            b'1 (X-GM-LABELS ("one")UID 9)',
        ):
            self.assertFalse(uid_fetch_exists([malformed], "9"))
        self.assertIsNone(select_uid_metadata([
            b"1 (INTERNALDATE " + date + b"UID 9)"], "9", required_item="INTERNALDATE"))

    def test_fetch_syntax_sp_is_ascii_space_not_other_whitespace(self):
        date = b'"02-Jan-2024 00:00:00 +0000"'
        for separator in (b"\t", b"\r", b"\n", b"\v", b"\f"):
            self.assertFalse(uid_fetch_exists([b"1" + separator + b"(UID 9)"], "9"))
            self.assertFalse(uid_fetch_exists([b"1 (UID" + separator + b"9)"], "9"))
            self.assertIsNone(select_uid_metadata([
                b"1 (UID 9" + separator + b"INTERNALDATE " + date + b")"], "9",
                required_item="INTERNALDATE"))
            self.assertIsNone(select_uid_metadata([
                b"1 (INTERNALDATE " + date + separator + b"UID 9)"], "9",
                required_item="INTERNALDATE"))
            self.assertFalse(uid_fetch_exists([b"1 (UID 9)" + separator], "9"))
        self.assertTrue(uid_fetch_exists([
            b'1 (INTERNALDATE "quoted\tcontent" UID 9)'], "9"))
        self.assertTrue(uid_fetch_exists([b"1 (UID 9)   "], "9"))

    def test_nested_literal_markers_and_duplicate_required_or_target_items_fail_closed(self):
        date = b'"02-Jan-2024 00:00:00 +0000"'
        nested_marker = (b'1 (UID 9 BODYSTRUCTURE ("TEXT" "PLAIN" ("NAME" {5}) '
                         b'NIL NIL "7BIT" 5 1) INTERNALDATE ' + date + b")")
        self.assertIsNone(select_uid_metadata([nested_marker], "9", required_item="BODYSTRUCTURE"))
        self.assertFalse(uid_fetch_exists([b"1 (UID 9 FLAGS ({5}))"], "9"))
        self.assertIsNone(select_uid_metadata([
            b'1 (UID 9 BODYSTRUCTURE ("TEXT" "PLAIN" ("NAME" {bad}) NIL NIL "7BIT" 5 1))'],
            "9", required_item="BODYSTRUCTURE"))
        self.assertFalse(uid_fetch_exists([
            b"1 (UID 9 FLAGS ({" + b"9" * 5000 + b"}))"], "9"))
        full = [(
            b'1 (UID 9 BODYSTRUCTURE ("TEXT" "PLAIN" ("NAME" {5}) NIL NIL "7BIT" 5 1) BODY[] {5}',
            b"hello"), b")"]
        self.assertIsNone(select_uid_fetch(full, "9"))
        section = [(
            b'1 (UID 9 FLAGS ({5}) BODY[1]<0> {5}', b"hello"), b")"]
        self.assertIsNone(select_uid_section(section, "9", "1", expected_length=5))

        quoted = (b'1 (UID 9 BODYSTRUCTURE ("TEXT" "PLAIN" ("NAME" "{5}") '
                  b'NIL NIL "7BIT" 5 1) INTERNALDATE ' + date + b")")
        self.assertIsNotNone(select_uid_metadata([quoted], "9", required_item="BODYSTRUCTURE"))
        self.assertTrue(uid_fetch_exists([b'1 (UID 9 FLAGS ("{5}"))'], "9"))
        full = [(
            b'1 (UID 9 BODYSTRUCTURE ("TEXT" "PLAIN" ("NAME" "{5}") NIL NIL "7BIT" 5 1) BODY[] {5}',
            b"hello"), b")"]
        self.assertEqual(select_uid_fetch(full, "9").raw, b"hello")

        self.assertIsNone(select_uid_fetch([(b"1 (UID 9 BODY[] NIL BODY[] {5}", b"hello"), b")"], "9"))
        self.assertIsNone(select_uid_section([(
            b"1 (UID 9 BODY[1]<0> NIL BODY[1]<0> {5}", b"hello"), b")"],
                                              "9", "1", expected_length=5))
        self.assertIsNone(select_uid_metadata([
            b"1 (UID 9 INTERNALDATE " + date + b" INTERNALDATE " + date + b")"], "9",
            required_item="INTERNALDATE"))
        self.assertIsNone(select_uid_metadata([
            b"1 (UID 9 BODYSTRUCTURE (NIL) BODYSTRUCTURE (NIL))"], "9",
            required_item="BODYSTRUCTURE"))
    def test_pagination_fetches_metadata_for_all_but_headers_only_for_page(self):
        search = load("search_emails.py")
        messages = [FakeMessage(str(index), sample_message(subject=str(index)), internaldate=f"{index:02d}-Jan-2024 12:00:00 +0000")
                    for index in range(1, 21)]
        imap = FakeIMAP({"INBOX": FakeMailbox(messages=messages)})
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=imap):
            result = search.query_emails("me@example.test", "token", limit=1)
        self.assertEqual(result["total"], 1)
        fetches = [call[3] for call in imap.log if call[:2] == ("UID", "FETCH")]
        self.assertEqual(fetches.count("(UID INTERNALDATE)"), 20)
        self.assertEqual(sum("HEADER.FIELDS" in query for query in fetches), 1)
        self.assertEqual(sum("<0.8192>" in query for query in fetches), 1)

    def test_list_fetches_only_headers_structure_and_bounded_plain_part(self):
        search = load("search_emails.py")
        huge = b"x" * (2 * 1024 * 1024)
        raw = sample_message(subject="listed", body="visible", attachment=("huge.bin", huge))
        imap = FakeIMAP({"INBOX": FakeMailbox(messages=[FakeMessage("9", raw)])})
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=imap):
            result = search.query_emails("me@example.test", "token")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["emails"][0]["subject"], "listed")
        self.assertEqual(result["emails"][0]["preview"], "visible\n")
        fetches = [call[3] for call in imap.log if call[:2] == ("UID", "FETCH")]
        self.assertEqual(len(fetches), 3)
        self.assertEqual(fetches[0], "(UID INTERNALDATE)")
        self.assertIn("HEADER.FIELDS", fetches[1])
        self.assertIn("BODYSTRUCTURE", fetches[1])
        self.assertNotIn("BODY.PEEK[]", fetches[1])
        self.assertNotIn("RFC822", fetches[1].upper())
        self.assertIn("BODY.PEEK[1]<0.8192>", fetches[2])
        self.assertNotIn("BODY[TEXT]", fetches[2])

    def test_shared_mime_helpers_tolerate_html_unknown_charset_and_malformed_structure(self):
        message = BytesParser().parsebytes(
            b"Subject: =?x-unknown?Q?hello?=\r\nContent-Type: text/html; charset=x-unknown\r\n\r\n<b>hi</b>")
        body, attachments = extract_body_and_attachments(message)
        self.assertEqual(decode_header_value(message["Subject"]), "hello")
        self.assertEqual(body, "<b>hi</b>")
        self.assertEqual(attachments, [])
        self.assertEqual(plain_text_part(b'BODYSTRUCTURE ("TEXT" "PLAIN" NIL NIL NIL "8BIT" 2 1 NIL NIL NIL)'), "1")
        self.assertIsNone(plain_text_part(b"BODYSTRUCTURE (broken"))
        self.assertEqual(decode_transfer(b"5L2g5aW9"[:-1], "base64", partial=True), b"\xe4\xbd\xa0")
        with self.assertRaises(ValueError):
            decode_transfer(b"QUJD!!!!REVG", "base64")

    def test_bodystructure_keeps_utf8_rfc2231_and_message_attachment_disposition(self):
        metadata = ('BODYSTRUCTURE ("MESSAGE" "RFC822" ("NAME*" "utf-8\'\'%E6%8A%A5%E5%91%8A.eml") NIL NIL "7BIT" 10 '
                    'NIL ("TEXT" "PLAIN" NIL NIL NIL "7BIT" 1 1 NIL NIL NIL) 1 NIL '
                    '("ATTACHMENT" ("FILENAME*" "utf-8\'\'%E6%8A%A5%E5%91%8A.eml")) NIL NIL)').encode("utf-8")
        part = bodystructure_parts(metadata)[0]
        self.assertTrue(part["attachment"])
        self.assertEqual(part["filename"], "报告.eml")

    def test_rfc2231_continuations_sort_numerically_and_reject_unsafe_series(self):
        def structure(params):
            return ('BODYSTRUCTURE ("APPLICATION" "OCTET-STREAM" (' + params +
                    ') NIL NIL "BASE64" 4 NIL ("ATTACHMENT" NIL) NIL NIL)').encode()

        segments = ['"FILENAME*0*" "utf-8\'\'0-"']
        segments.extend(f'"FILENAME*{index}*" "{index}-"' for index in range(1, 11))
        segments.append('"FILENAME*11*" "11.txt"')
        self.assertEqual(bodystructure_parts(structure(" ".join(segments)))[0]["filename"],
                         "0-1-2-3-4-5-6-7-8-9-10-11.txt")
        self.assertIsNone(bodystructure_parts(structure(
            '"FILENAME*0*" "utf-8\'\'first" "FILENAME*2*" "last"'))[0]["filename"])
        self.assertIsNone(bodystructure_parts(structure(
            '"FILENAME*0*" "utf-8\'\'first" "FILENAME*0*" "replacement"'))[0]["filename"])
        self.assertIsNone(bodystructure_parts(structure(
            '"FILENAME*0*" "missing-charset" "FILENAME*1*" "tail"'))[0]["filename"])
        self.assertEqual(bodystructure_parts(structure(
            '"FILENAME*0" "100%" "FILENAME*1" "2Etxt"'))[0]["filename"], "100%2Etxt")
        self.assertIsNone(bodystructure_parts(structure(
            '"FILENAME*0*" "utf-8\'\'one" "FILENAME*1" "%20two"'))[0]["filename"])

    def test_complete_invalid_quoted_printable_body_fails_search_and_detail(self):
        raw = (b"Subject: broken QP\r\nFrom: sender@example.test\r\nTo: me@example.test\r\n"
               b"Content-Type: text/plain; charset=utf-8\r\n"
               b"Content-Transfer-Encoding: quoted-printable\r\n\r\nabc=GGdef")
        mailbox = {"INBOX": FakeMailbox(messages=[FakeMessage("9", raw)])}
        search = load("search_emails.py")
        imap = FakeIMAP(mailbox)
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=imap):
            listed = search.query_emails("me@example.test", "token", limit=1)
        self.assertEqual((listed["status"], listed["total"], listed["emails"]), ("error", 0, []))
        detail = load("get_email.py")
        imap = FakeIMAP(mailbox)
        with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=imap):
            fetched = detail.get_emails("me@example.test", "token", ["9"], "INBOX", "1")
        self.assertEqual((fetched["status"], fetched["fetched"]), ("error", 0))

    def test_bodystructure_depth_and_token_limits_are_non_recursive(self):
        deeply_nested = b"BODYSTRUCTURE " + b"(" * 998 + b"NIL" + b")" * 998
        self.assertEqual(bodystructure_parts(deeply_nested), [])
        near_token_limit = b"BODYSTRUCTURE (" + b" ".join([b"NIL"] * 2_001) + b")"
        self.assertEqual(bodystructure_parts(near_token_limit), [])


class AttachmentHardeningTests(OfflineTestCase):
    def test_zero_wire_attachment_and_utf8_filename_are_downloaded_without_invalid_fetch(self):
        download = load("download_attachment.py")
        with tempfile.TemporaryDirectory() as directory:
            imap = FakeIMAP({"INBOX": FakeMailbox(messages=[
                FakeMessage("1", sample_message(attachment=("报告.pdf", b""))),
            ])})
            result = download.download_attachments_for_mail(imap, MailRef("INBOX", "1", "1"), directory)
            self.assertEqual(result["downloaded"][0]["name"], "报告.pdf")
            self.assertEqual((pathlib.Path(directory) / "报告.pdf").read_bytes(), b"")
            self.assertFalse(any("<0.0>" in call[3] for call in imap.log if call[:2] == ("UID", "FETCH")))

    def test_zero_wire_attachment_can_publish_when_shared_decoded_budget_is_empty(self):
        download = load("download_attachment.py")
        with tempfile.TemporaryDirectory() as directory:
            imap = FakeIMAP({"INBOX": FakeMailbox(messages=[
                FakeMessage("1", sample_message(attachment=("empty.bin", b""))),
            ])})
            result = download.download_attachments_for_mail(
                imap, MailRef("INBOX", "1", "1"), directory,
                budget={"remaining": 0, "wire_remaining": download.MAX_DOWNLOAD_WIRE_BYTES})
            self.assertEqual((result["status"], result["downloaded"][0]["size"]), ("success", 0))
            self.assertEqual((pathlib.Path(directory) / "empty.bin").read_bytes(), b"")
    def test_incremental_base64_and_qp_decode_bound_writes_and_reject_truncation(self):
        download = load("download_attachment.py")
        payload = b"x" * (2 * 1024 * 1024)
        encoded = __import__("base64").b64encode(payload)
        writes = []
        original = download._LimitedWriter
        class RecordingWriter(original):
            def write(self, block):
                writes.append(len(block))
                return super().write(block)
        with tempfile.TemporaryDirectory() as directory, patch.object(download, "_LimitedWriter", RecordingWriter):
            target = pathlib.Path(directory) / "base64.bin"
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            self.assertEqual(download._decode_to_file(encoded, fd, len(payload), "base64"), len(payload))
            os.close(fd)
            self.assertLessEqual(max(writes), 64 * 1024)
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), hashlib.sha256(payload).hexdigest())
            for malformed in (b"a", b"abc", b"QUJD!!!!REVG"):
                temp = pathlib.Path(directory) / ("bad-" + str(len(malformed)))
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with self.assertRaises(Exception):
                    download._decode_to_file(malformed, fd, 100, "base64")
                os.close(fd)
                temp.unlink(missing_ok=True)
            padded_then_data = b"A" * 65534 + b"==" + b"Qg=="
            temp = pathlib.Path(directory) / "padding-after-data"
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with self.assertRaises(ValueError):
                download._decode_to_file(padded_then_data, fd, 100000, "base64")
            os.close(fd)
            temp.unlink(missing_ok=True)
            qp = b"A" * (64 * 1024 - 2) + b"=3Dsoft=\r\nline"
            target = pathlib.Path(directory) / "quoted.bin"
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            download._decode_to_file(qp, fd, len(qp), "quoted-printable")
            os.close(fd)
            self.assertEqual(target.read_bytes(), b"A" * (64 * 1024 - 2) + b"=softline")
            for malformed in (b"bad=", b"bad=3", b"abc=GGdef"):
                temp = pathlib.Path(directory) / ("qp-" + str(len(malformed)))
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with self.assertRaises(ValueError):
                    download._decode_to_file(malformed, fd, 100, "quoted-printable")
                os.close(fd)
                temp.unlink(missing_ok=True)

    def test_hardlink_fallback_and_unlink_warning_keep_final_file_accurate(self):
        download = load("download_attachment.py")
        with tempfile.TemporaryDirectory() as directory:
            folder = pathlib.Path(directory)
            with patch.object(download.os, "link", side_effect=OSError(errno.EOPNOTSUPP, "unsupported")):
                result = download._save_part(b"data", "8bit", folder, "same.txt", 10)
            self.assertEqual(result[0].read_bytes(), b"data")
            victim = folder / "victim.txt"
            victim.write_bytes(b"victim")
            with patch.object(download.os, "link", side_effect=OSError(errno.EOPNOTSUPP, "unsupported")):
                second = download._save_part(b"other", "8bit", folder, "same.txt", 10)
            self.assertEqual(second[0].name, "same_1.txt")
            self.assertEqual(victim.read_bytes(), b"victim")

            real_unlink = pathlib.Path.unlink
            def fail_temp_unlink(path, *args, **kwargs):
                if path.name.endswith(".part"):
                    raise OSError("injected cleanup failure")
                return real_unlink(path, *args, **kwargs)
            with patch.object(pathlib.Path, "unlink", fail_temp_unlink):
                published, size, warning = download._save_part(b"ok", "8bit", folder, "warn.txt", 10)
            self.assertEqual((published.read_bytes(), size), (b"ok", 2))
            self.assertIsNotNone(warning)

            with patch.object(download.os, "link", side_effect=OSError(errno.EOPNOTSUPP, "unsupported")), \
                    patch.object(download.os, "dup", side_effect=OSError(errno.EMFILE, "full")):
                with self.assertRaises(OSError):
                    download._save_part(b"fail", "8bit", folder, "emfile.txt", 10)
            self.assertFalse((folder / "emfile.txt").exists())

    def test_save_part_has_single_fd_owner_across_success_and_failures(self):
        download = load("download_attachment.py")
        with tempfile.TemporaryDirectory() as directory:
            folder = pathlib.Path(directory)

            def close_count(action):
                real_close, calls = download.os.close, []
                def tracked(fd):
                    calls.append(fd)
                    return real_close(fd)
                try:
                    with patch.object(download.os, "close", side_effect=tracked):
                        action()
                finally:
                    self.assertEqual(len(calls), 1)

            close_count(lambda: download._save_part(b"ok", "8bit", folder, "success.bin", 10))
            with self.assertRaises(ValueError):
                close_count(lambda: download._save_part(b"!", "base64", folder, "decode.bin", 10))
            with self.assertRaises(OSError), patch.object(download.os, "fdopen", side_effect=OSError("fdopen")):
                close_count(lambda: download._save_part(b"ok", "8bit", folder, "fdopen.bin", 10))
            with self.assertRaises(OSError), patch.object(download, "_commit_without_overwrite", side_effect=OSError("publish")):
                close_count(lambda: download._save_part(b"ok", "8bit", folder, "publish.bin", 10))
            self.assertFalse(list(folder.glob(".qqmail-*.part")))

    def test_decode_failure_cannot_double_close_a_reused_fd(self):
        download = load("download_attachment.py")
        with tempfile.TemporaryDirectory() as directory:
            folder, sentinel = pathlib.Path(directory), pathlib.Path(directory) / "unrelated.bin"
            real_close, calls, holder = download.os.close, [], {}
            def close_then_reuse(fd):
                calls.append(fd)
                real_close(fd)
                if len(calls) == 1:
                    replacement = os.open(sentinel, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
                    self.assertEqual(replacement, fd)
                    holder["fd"] = replacement
            with patch.object(download.os, "close", side_effect=close_then_reuse):
                with self.assertRaises(ValueError):
                    download._save_part(b"!", "base64", folder, "bad.bin", 10)
            self.assertEqual(len(calls), 1)
            os.write(holder["fd"], b"safe")
            os.lseek(holder["fd"], 0, os.SEEK_SET)
            self.assertEqual(os.read(holder["fd"], 4), b"safe")
            os.close(holder["fd"])
            self.assertFalse(list(folder.glob(".qqmail-*.part")))

    def test_hardlink_fallback_rejects_same_size_path_replacement(self):
        download = load("download_attachment.py")
        with tempfile.TemporaryDirectory() as directory:
            folder = pathlib.Path(directory)
            destination, victim = folder / "target.bin", folder / "victim.bin"
            victim.write_bytes(b"EVIL")
            real_stat, real_link, replaced = download.os.stat, os.link, {"done": False}
            def replace_after_close(path, *args, **kwargs):
                if pathlib.Path(path) == destination and not replaced["done"]:
                    replaced["done"] = True
                    destination.unlink(missing_ok=True)
                    real_link(victim, destination)
                return real_stat(path, *args, **kwargs)
            with patch.object(download.os, "link", side_effect=OSError(errno.EOPNOTSUPP, "unsupported")), \
                    patch.object(download.os, "stat", side_effect=replace_after_close):
                with self.assertRaises(OSError):
                    download._save_part(b"GOOD", "8bit", folder, "target.bin", 10)
            self.assertTrue(replaced["done"])
            self.assertEqual((victim.read_bytes(), destination.read_bytes()), (b"EVIL", b"EVIL"))
            self.assertFalse(list(folder.glob(".qqmail-*.part")))

    def test_hardlink_fallback_close_errors_cannot_close_reused_source_or_destination_fd(self):
        download = load("download_attachment.py")
        with tempfile.TemporaryDirectory() as directory:
            folder = pathlib.Path(directory)

            def exercise(which):
                target, unrelated = folder / f"{which}.bin", folder / f"{which}-unrelated.bin"
                real_close, real_open, real_dup = download.os.close, download.os.open, download.os.dup
                handles, reused = {}, {}
                def record_open(path, *args, **kwargs):
                    value = real_open(path, *args, **kwargs)
                    if pathlib.Path(path) == target:
                        handles["destination"] = value
                    return value
                def record_dup(fd):
                    value = real_dup(fd)
                    handles["source"] = value
                    return value
                def close_then_reuse(fd):
                    if fd == handles.get(which) and "fd" not in reused:
                        real_close(fd)
                        replacement = real_open(unrelated, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
                        self.assertEqual(replacement, fd)
                        reused["fd"] = replacement
                        raise OSError(errno.EINTR, "injected close error")
                    return real_close(fd)
                with patch.object(download.os, "link", side_effect=OSError(errno.EOPNOTSUPP, "unsupported")), \
                        patch.object(download.os, "open", side_effect=record_open), \
                        patch.object(download.os, "dup", side_effect=record_dup), \
                        patch.object(download.os, "close", side_effect=close_then_reuse):
                    with self.assertRaises(OSError):
                        download._save_part(b"GOOD", "8bit", folder, target.name, 10)
                self.assertIn("fd", reused)
                os.write(reused["fd"], b"safe")
                os.lseek(reused["fd"], 0, os.SEEK_SET)
                self.assertEqual(os.read(reused["fd"], 4), b"safe")
                os.close(reused["fd"])
                self.assertFalse(target.exists())
                self.assertTrue(unrelated.exists())
                self.assertFalse(list(folder.glob(".qqmail-*.part")))

            exercise("source")
            exercise("destination")

    def test_temp_replacement_race_fails_without_unlinking_foreign_inode(self):
        download = load("download_attachment.py")
        with tempfile.TemporaryDirectory() as directory:
            folder = pathlib.Path(directory)
            victim = folder / "outside-victim.txt"
            victim.write_bytes(b"do-not-touch")
            real_link, real_unlink = download.os.link, pathlib.Path.unlink
            def replace_before_publish(source, destination):
                source_path = pathlib.Path(source)
                real_unlink(source_path)
                real_link(victim, source_path)
                return real_link(source_path, destination)
            with patch.object(download.os, "link", side_effect=replace_before_publish):
                with self.assertRaises(OSError):
                    download._save_part(b"new", "8bit", folder, "target.txt", 10)
            self.assertEqual(victim.read_bytes(), b"do-not-touch")
            target = folder / "target.txt"
            if target.exists():
                self.assertTrue(os.path.samefile(target, victim))
                real_unlink(target)
            self.assertFalse(list(folder.glob(".qqmail-*.part")))

            # Windows cannot unlink the open temporary file above, so force
            # the same post-link foreign-identity state on every platform.
            def publish_foreign_inode(_source, destination):
                return real_link(victim, destination)
            with patch.object(download.os, "link", side_effect=publish_foreign_inode):
                with self.assertRaisesRegex(OSError, "附件发布身份校验失败"):
                    download._save_part(b"new", "8bit", folder, "portable-target.txt", 10)
            self.assertEqual(victim.read_bytes(), b"do-not-touch")
            portable_target = folder / "portable-target.txt"
            self.assertTrue(os.path.samefile(portable_target, victim))
            real_unlink(portable_target)
            self.assertFalse(list(folder.glob(".qqmail-*.part")))

    def test_attachment_disposition_without_filename_downloads_stable_fallback(self):
        download = load("download_attachment.py")
        class NoNameIMAP(FakeIMAP):
            def uid(self, command, *args):
                if command.upper() == "FETCH" and len(args) > 1 and str(args[1]) == "(UID BODYSTRUCTURE)":
                    return "OK", [b'1 (UID 1 BODYSTRUCTURE ("APPLICATION" "OCTET-STREAM" NIL NIL NIL "BASE64" 4 NIL ("ATTACHMENT" NIL) NIL NIL))']
                if command.upper() == "FETCH" and len(args) > 1 and "BODY.PEEK[1]" in str(args[1]):
                    return "OK", [(b"1 (UID 1 BODY[1]<0>)", b"eA==")]
                return super().uid(command, *args)
        with tempfile.TemporaryDirectory() as directory:
            imap = NoNameIMAP({"INBOX": FakeMailbox(messages=[FakeMessage("1", sample_message())])})
            result = download.download_attachments_for_mail(imap, MailRef("INBOX", "1", "1"), directory)
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["downloaded"][0]["name"], "attachment-1")
            self.assertEqual((pathlib.Path(directory) / "attachment-1").read_bytes(), b"x")
    def test_total_budget_is_shared_across_uids_and_partial_surfaces_at_top_level(self):
        download = load("download_attachment.py")
        with tempfile.TemporaryDirectory() as directory, patch.object(download, "MAX_ATTACHMENT_BYTES", 10), patch.object(download, "MAX_DOWNLOAD_BYTES", 8):
            imap = FakeIMAP({"INBOX": FakeMailbox(messages=[
                FakeMessage("1", sample_message(attachment=("one.bin", b"12345"))),
                FakeMessage("2", sample_message(attachment=("two.bin", b"67890"))),
            ])})
            with patch("qqmail_core.connections.imaplib.IMAP4_SSL", return_value=imap):
                result = download.download_attachments("me@example.test", "token", ["1", "2"], "INBOX", "1", directory)
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["total_downloaded"], 1)
            self.assertEqual((pathlib.Path(directory) / "one.bin").read_bytes(), b"12345")
            self.assertFalse((pathlib.Path(directory) / "two.bin").exists())
    def test_windows_filename_cases_and_existing_file_never_overwrite(self):
        download = load("download_attachment.py")
        expected = {"CON.txt": "_CON.txt", "aux ": "_aux", "..\\..\\x.txt": "x.txt",
                    "\\\\server\\share\\x.txt": "x.txt", "a.txt:evil": "a.txt_evil", "...": "attachment"}
        self.assertEqual({name: download.safe_filename(name) for name in expected}, expected)
        with tempfile.TemporaryDirectory() as directory:
            existing = pathlib.Path(directory) / "same.txt"
            existing.write_bytes(b"original")
            imap = FakeIMAP({"INBOX": FakeMailbox(messages=[FakeMessage("1", sample_message(attachment=("same.txt", b"new")))])})
            outcome = download.download_attachments_for_mail(imap, MailRef("INBOX", "1", "1"), directory)
            self.assertEqual(outcome["status"], "success")
            self.assertEqual(existing.read_bytes(), b"original")
            self.assertEqual((pathlib.Path(directory) / "same_1.txt").read_bytes(), b"new")

    def test_bidi_and_long_emoji_names_stay_within_windows_utf16_limit(self):
        download = load("download_attachment.py")
        name = download.safe_filename("\u202e" + "😀" * 400 + "." + "x" * 400)
        self.assertNotIn("\u202e", name)
        self.assertLessEqual(len(name.encode("utf-16-le")) // 2, download.MAX_FILENAME_UTF16_UNITS)

    def test_size_failures_are_bounded_partial_and_leave_no_temp_files(self):
        download = load("download_attachment.py")
        with tempfile.TemporaryDirectory() as directory, patch.object(download, "MAX_ATTACHMENT_BYTES", 5), patch.object(download, "MAX_DOWNLOAD_BYTES", 8):
            imap = FakeIMAP({"INBOX": FakeMailbox(messages=[FakeMessage("1", two_attachments())])})
            outcome = download.download_attachments_for_mail(imap, MailRef("INBOX", "1", "1"), directory)
            self.assertEqual(outcome["status"], "partial")
            self.assertEqual(outcome["download_count"], 1)
            self.assertEqual(len(outcome["attachment_failed"]), 1)
            self.assertFalse(list(pathlib.Path(directory).glob(".qqmail-*.part")))
            self.assertEqual((pathlib.Path(directory) / "same.txt").read_bytes(), b"small")

    def test_simultaneous_same_name_downloads_use_distinct_atomic_targets(self):
        download = load("download_attachment.py")
        barrier = threading.Barrier(2)
        with tempfile.TemporaryDirectory() as directory:
            def one(uid):
                imap = FakeIMAP({"INBOX": FakeMailbox(messages=[FakeMessage(uid, sample_message(attachment=("same.txt", b"data")))])})
                barrier.wait()
                return download.download_attachments_for_mail(imap, MailRef("INBOX", "1", uid), directory)
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(one, ("1", "2")))
            self.assertEqual([item["status"] for item in outcomes], ["success", "success"])
            names = {item["downloaded"][0]["name"] for item in outcomes}
            self.assertEqual(names, {"same.txt", "same_1.txt"})
            self.assertEqual({path.read_bytes() for path in pathlib.Path(directory).glob("same*.txt")}, {b"data"})

    def test_invalid_static_output_path_returns_one_json_before_credentials(self):
        download = load("download_attachment.py")
        with tempfile.TemporaryDirectory() as directory:
            invalid = pathlib.Path(directory) / "not-a-directory"
            invalid.write_text("x", encoding="utf-8")
            output = io.StringIO()
            with patch.dict(os.environ, {}, clear=True), patch.object(sys, "argv", [
                "download_attachment.py", "--mail_ids", "1", "--folder", "INBOX", "--uidvalidity", "1", "--dir", str(invalid)]), contextlib.redirect_stdout(output):
                self.assertEqual(download.main(), 1)
            lines = output.getvalue().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["code"], "invalid_download_path")

            nested = invalid / "child"
            output = io.StringIO()
            with patch.dict(os.environ, {}, clear=True), patch.object(sys, "argv", [
                "download_attachment.py", "--mail_ids", "1", "--folder", "INBOX", "--uidvalidity", "1", "--dir", str(nested)]), contextlib.redirect_stdout(output):
                self.assertEqual(download.main(), 1)
            self.assertEqual(json.loads(output.getvalue())["code"], "invalid_download_path")

    def test_download_help_advertises_default_quotas(self):
        download = load("download_attachment.py")
        output = io.StringIO()
        with patch.object(sys, "argv", ["download_attachment.py", "--help"]), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as stopped:
                download.main()
        self.assertEqual(stopped.exception.code, 0)
        self.assertIn("50MiB", output.getvalue())
        self.assertIn("100MiB", output.getvalue())
