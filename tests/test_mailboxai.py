"""MailboxAI repo tests — stdlib unittest, no keys, no network.

Run from the repo root:

    python3 -m unittest discover -s tests -v

Everything here runs against a throwaway SQLite file and mocked
WhatsApp sends, so it is safe to run anywhere. The suite covers the
three things judges are most likely to poke: the license expiry gate,
the household join alert, and the rule that nothing is ever lost to
a filter failure (Spark unset -> rules engine, never an exception).
"""

import asyncio
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config

_TMPDIR = tempfile.mkdtemp(prefix="mailboxai-test-")
# Set BEFORE importing app.main: main calls state.init_db() on import,
# and that must land in the throwaway file, never a real mailboxai.db.
config.DB_PATH = os.path.join(_TMPDIR, "test.db")

from app import license as license_mod
from app import digest as digest_mod
from app import main, state, triage
from app import voice_in as voice_in_mod

ADDR = "999 Sample Road, Atlanta, GA 30309"
PHONE1 = "+15550000001"
PHONE2 = "+15550000002"


def tearDownModule():
    shutil.rmtree(_TMPDIR, ignore_errors=True)


class DbTestCase(unittest.TestCase):
    def setUp(self):
        state.init_db()
        with state._conn() as c:
            c.execute("DELETE FROM users")
            c.execute("DELETE FROM mail")


class TestExpiryParsing(DbTestCase):
    def test_formats(self):
        self.assertEqual(license_mod.parse_expiry_date("11/15/2026"),
                         date(2026, 11, 15))
        self.assertEqual(license_mod.parse_expiry_date("11-15-2026"),
                         date(2026, 11, 15))
        self.assertEqual(license_mod.parse_expiry_date("2028-05-01"),
                         date(2028, 5, 1))
        self.assertEqual(license_mod.parse_expiry_date(" 11/15/2026 "),
                         date(2026, 11, 15))

    def test_unparseable_is_none(self):
        for value in ("May 1, 2028", "13/40/2026", "", None, 20261115):
            self.assertIsNone(license_mod.parse_expiry_date(value))


class TestIsExpired(DbTestCase):
    def test_past_future_today(self):
        today = date(2026, 11, 1)
        self.assertTrue(license_mod.is_expired("10/31/2026", today=today))
        self.assertFalse(license_mod.is_expired("11/01/2026", today=today))
        self.assertFalse(license_mod.is_expired("11/02/2026", today=today))

    def test_unparseable_never_rejects(self):
        today = date(2026, 11, 1)
        for value in (None, "", "May 1, 2020", "13/40/2020"):
            self.assertFalse(license_mod.is_expired(value, today=today))


class TestNormalizeCode(DbTestCase):
    def test_words_dashes_sentence(self):
        self.assertEqual(main._normalize_code("seven three nine one"),
                         "7391")
        self.assertEqual(main._normalize_code("7-3-9-1"), "7391")
        self.assertEqual(main._normalize_code("My code is 7 3 9 1, thanks"),
                         "7391")
        self.assertEqual(main._normalize_code("oh seven three nine one"),
                         "07391")

    def test_word_boundaries(self):
        # "seventy" must not leak digits: word boundaries only.
        self.assertEqual(main._normalize_code("seventy three ninety one"),
                         "31")


class TestRulesTriageFallback(DbTestCase):
    def test_no_keys_means_rules_never_exception(self):
        with mock.patch.object(config, "META_MODEL_API_BASE_URL", ""), \
                mock.patch.object(config, "META_MODEL_API_KEY", ""), \
                mock.patch.object(config, "FIREWORKS_API_KEY", ""), \
                mock.patch.object(config, "FIREWORKS_MODEL", ""):
            bill = triage.triage_letter(
                "First Demo Bank",
                "Your statement is ready. Balance $412.30, payment due "
                "November 15, 2026.")
            self.assertEqual(bill["category"], "bill")
            self.assertEqual(bill["amount"], "$412.30")
            self.assertEqual(bill["due"], "November 15, 2026")
            self.assertEqual(bill["engine"], "rules")
            junk = triage.triage_letter(
                "ShopMart",
                "Big clearance sale, 50% off everything, unsubscribe here.")
            self.assertEqual(junk["category"], "junk")
            self.assertEqual(junk["engine"], "rules")


class TestHouseholdJoinAlert(DbTestCase):
    def setUp(self):
        super().setUp()
        state.save_user(PHONE1, name="Sagar Gharge", address=ADDR,
                        mailbox=ADDR, state="VERIFIED")

    def test_join_alerts_existing_member(self):
        with mock.patch.object(main.whatsapp, "send_text") as send_mock:
            reply = main._complete_verification(
                PHONE2, {"phone": PHONE2}, "Priyanka Gharge", ADDR)
        self.assertIn("joined", reply)
        send_mock.assert_called_once()
        args = send_mock.call_args.args
        self.assertEqual(args[0], PHONE1)
        self.assertIn("Priyanka Gharge", args[1])
        user2 = state.get_user(PHONE2)
        self.assertEqual(user2["mailbox"], ADDR)
        self.assertEqual(user2["state"], "VERIFIED")

    def test_failed_alert_never_blocks_verification(self):
        with mock.patch.object(main.whatsapp, "send_text",
                               side_effect=Exception("no network")):
            reply = main._complete_verification(
                PHONE2, {"phone": PHONE2}, "Priyanka Gharge", ADDR)
        self.assertIn("joined", reply)
        self.assertEqual(state.get_user(PHONE2)["state"], "VERIFIED")


class TestExpiryGate(DbTestCase):
    def _handle(self, expiry):
        user = {"phone": PHONE1, "state": "AWAITING_LICENSE",
                "name": "Sagar Gharge"}
        fake_result = {"ok": True, "name": "Sagar Gharge",
                       "address": ADDR, "expiry": expiry}
        with mock.patch.object(main.whatsapp, "download_media",
                               return_value=b"fake-bytes"), \
                mock.patch.object(main.license_mod, "verify_license",
                                  return_value=fake_result), \
                mock.patch.object(config, "MOCK_VERIFY", False):
            return main._handle_image(PHONE1, user, "media123")

    def test_expired_license_rejected(self):
        reply = self._handle("01/01/2020")
        self.assertIn("expired", reply)
        self.assertIsNone(state.get_user(PHONE1))

    def test_valid_license_proceeds_to_challenge(self):
        reply = self._handle("01/01/2030")
        self.assertIn("Last step for security", reply)


class TestSampleCardOcr(DbTestCase):
    """The rebuilt sample card: no-state header, real future expiry.

    OCR text mimics what Tesseract reads off the new card (Spark is
    unconfigured in tests, so verify_license falls through to OCR).
    """
    CARD_TEXT = (
        "DRIVER'S LICENSE\nSAMPLE\nGHARGE, SAGAR\n"
        "999 Sample Road, Atlanta, GA 30309\n"
        "DOB **/** ****\nDL NO **\nEXP 12/31/2030\n"
        "SAMPLE - NOT A REAL LICENSE - DEMO ONLY")

    def _verify(self, text):
        with mock.patch.object(license_mod, "_spark_attempt",
                               return_value=(None, None)), \
                mock.patch.object(license_mod, "_ocr_passes",
                                  return_value=[text]), \
                mock.patch.object(config, "MOCK_VERIFY", False):
            return license_mod.verify_license(b"fake-bytes",
                                              expected_name="Sagar Gharge")

    def test_new_card_yields_future_expiry(self):
        result = self._verify(self.CARD_TEXT)
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "Sagar Gharge")
        self.assertEqual(result["address"],
                         "999 Sample Road, Atlanta, GA 30309")
        self.assertEqual(result["expiry"], "12/31/2030")
        self.assertFalse(license_mod.is_expired(result["expiry"]))
        self.assertNotIn("dob", result)
        self.assertNotIn("license_number", result)

    def test_expired_card_text_is_flagged(self):
        result = self._verify(self.CARD_TEXT.replace("12/31/2030",
                                                     "01/01/2020"))
        self.assertEqual(result["expiry"], "01/01/2020")
        self.assertTrue(license_mod.is_expired(result["expiry"]))


class TestChallengeLockout(DbTestCase):
    def setUp(self):
        super().setUp()
        main._CHALLENGES.clear()
        main._LICENSE_PHOTOS.clear()
        main._FAILED_ATTEMPTS.clear()
        main._LOCKOUTS.clear()
        main._LICENSE_PHOTOS[PHONE1] = b"fake-license"
        self.user = {"phone": PHONE1, "state": "AWAITING_SELFIE",
                     "name": "Sagar Gharge", "address": ADDR}

    def _video(self, liveness):
        main._issue_challenge(PHONE1)
        with mock.patch.object(main.whatsapp, "download_media",
                               return_value=b"fake-video"), \
                mock.patch.object(main.face_mod, "check_liveness",
                                  return_value=liveness) as live_mock:
            reply = main._handle_video(PHONE1, self.user, "media123")
        return reply, live_mock

    def test_five_failures_lock_and_sixth_never_verifies(self):
        dead = {"ok": True, "live": False, "reason": "no movement seen"}
        for i in range(4):
            reply, _ = self._video(dead)
            self.assertIn("Please retake it", reply)
            self.assertIn("Last step for security", reply)
        reply, live_mock = self._video(dead)
        self.assertIn("paused for 10 minutes", reply)
        self.assertNotIn("Last step for security", reply)
        self.assertTrue(main._is_locked(PHONE1))
        calls_after_lock = live_mock.call_count
        reply, _ = self._video(dead)
        self.assertIn("paused for 10 minutes", reply)
        # Locked: no further liveness check is even attempted.
        self.assertEqual(live_mock.call_count, calls_after_lock)

    def test_success_clears_failure_count(self):
        dead = {"ok": True, "live": False, "reason": "no movement seen"}
        for _ in range(4):
            self._video(dead)
        self.assertEqual(len(main._FAILED_ATTEMPTS[PHONE1]), 4)
        main._issue_challenge(PHONE1)
        code = main._CHALLENGES[PHONE1]["code"]
        with mock.patch.object(main.whatsapp, "download_media",
                               return_value=b"fake-video"), \
                mock.patch.object(main.face_mod, "check_liveness",
                                  return_value={"ok": True, "live": True}), \
                mock.patch.object(main.face_mod, "match_faces_best",
                                  return_value={"ok": True, "match": True}), \
                mock.patch.object(main.voice_in_mod, "transcribe_video",
                                  return_value={"ok": True, "text": code}):
            reply = main._handle_video(PHONE1, self.user, "media123")
        self.assertIn("spoken code verified", reply)
        self.assertNotIn(PHONE1, main._FAILED_ATTEMPTS)
        self.assertEqual(state.get_user(PHONE1)["state"], "VERIFIED")


class TestFaceBestFrame(unittest.TestCase):
    def test_first_match_wins(self):
        with mock.patch.object(main.face_mod, "match_faces",
                               side_effect=[
                                   {"ok": True, "match": False,
                                    "confidence": 30, "reason": "blink"},
                                   {"ok": True, "match": True,
                                    "confidence": 88, "reason": "match"}]) as m:
            result = main.face_mod.match_faces_best(b"lic", [b"f1", b"f2"])
        self.assertTrue(result["match"])
        self.assertEqual(m.call_count, 2)

    def test_all_mismatch_returns_highest_confidence(self):
        with mock.patch.object(main.face_mod, "match_faces",
                               side_effect=[
                                   {"ok": True, "match": False,
                                    "confidence": 20, "reason": "a"},
                                   {"ok": True, "match": False,
                                    "confidence": 55, "reason": "b"}]):
            result = main.face_mod.match_faces_best(b"lic", [b"f1", b"f2"])
        self.assertFalse(result["match"])
        self.assertEqual(result["confidence"], 55)

    def test_prompt_ignores_lighting_and_age(self):
        with mock.patch.object(main.face_mod.muse_client, "is_configured",
                               return_value=True), \
                mock.patch.object(main.face_mod.muse_client,
                                  "chat_with_images",
                                  return_value='{"same_person": true, '
                                  '"confidence": 90, "reason": "ok"}') as c:
            main.face_mod.match_faces(b"lic", b"selfie")
        prompt = c.call_args[0][0]
        self.assertIn("facial structure", prompt)
        self.assertIn("lighting", prompt)
        self.assertNotIn("Consider face shape, eyes, nose, mouth, skin tone and age",
                         prompt)


class TestMuseLogTags(unittest.TestCase):
    def _fake_urlopen(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = json.dumps(
            {"choices": [{"message": {"content": "ok"}}]}).encode()
        return resp

    def test_tagged_log_line(self):
        with mock.patch.object(config, "META_MODEL_API_BASE_URL",
                               "https://api.meta.ai/v1"), \
                mock.patch.object(config, "META_MODEL_API_KEY", "LLM_test"), \
                mock.patch("urllib.request.urlopen",
                           return_value=self._fake_urlopen()), \
                self.assertLogs("mailboxai.muse", level="INFO") as logs:
            main.face_mod.muse_client.chat("hello", tag="triage")
        self.assertIn("[muse] triage reply 2 chars", "\n".join(logs.output))

    def test_untagged_log_line_unchanged(self):
        with mock.patch.object(config, "META_MODEL_API_BASE_URL",
                               "https://api.meta.ai/v1"), \
                mock.patch.object(config, "META_MODEL_API_KEY", "LLM_test"), \
                mock.patch("urllib.request.urlopen",
                           return_value=self._fake_urlopen()), \
                self.assertLogs("mailboxai.muse", level="INFO") as logs:
            main.face_mod.muse_client.chat("hello")
        self.assertIn("[muse] reply 2 chars", "\n".join(logs.output))

    def test_call_sites_pass_tags(self):
        client = main.face_mod.muse_client
        with mock.patch.object(client, "chat",
                               return_value='{"category": "bill", '
                               '"amount": "$1", "due": null, '
                               '"scam": false}') as c:
            triage._spark_triage("Bank", "Statement text")
        self.assertEqual(c.call_args[1].get("tag"), "triage")
        with mock.patch.object(client, "is_configured",
                               return_value=True), \
                mock.patch.object(client, "chat_with_images",
                                  side_effect=Exception("boom")) as c:
            license_mod._spark_attempt(b"fake", "Sagar Gharge")
        self.assertEqual(c.call_args[1].get("tag"), "vision-license")
        with mock.patch.object(client, "is_configured",
                               return_value=True), \
                mock.patch.object(client, "chat_with_images",
                                  return_value='{"same_person": true, '
                                  '"confidence": 90, "reason": "ok"}') as c:
            main.face_mod.match_faces(b"lic", b"selfie")
        self.assertEqual(c.call_args[1].get("tag"), "vision-face")
        with mock.patch.object(main.face_mod, "extract_frames",
                               return_value=[b"f1", b"f2"]), \
                mock.patch.object(client, "is_configured",
                                  return_value=True), \
                mock.patch.object(client, "chat_with_images",
                                  return_value='{"live": true, '
                                  '"reason": "blink"}') as c:
            main.face_mod.check_liveness(b"video")
        self.assertEqual(c.call_args[1].get("tag"), "vision-liveness")
        from app import wonder as wonder_mod
        with mock.patch.object(client, "is_configured",
                               return_value=True), \
                mock.patch.object(client, "chat",
                                  return_value="short answer") as c:
            wonder_mod.answer_question("any bills?", [])
        self.assertEqual(c.call_args[1].get("tag"), "wonder")


class TestTranscribeRequestShape(unittest.TestCase):
    def test_request_json_names_model_and_wav_encoding(self):
        resp = mock.MagicMock()
        resp.read.return_value = b'{"transcript": "seven three nine one"}'
        resp.__enter__.return_value = resp
        with mock.patch.object(config, "META_MODEL_API_BASE_URL",
                               "https://api.meta.ai/v1"), \
                mock.patch.object(voice_in_mod.urllib.request, "urlopen",
                                  return_value=resp) as urlopen_mock:
            result = voice_in_mod._post_wav(b"fake-wav-bytes")
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "seven three nine one")
        sent = urlopen_mock.call_args[0][0].data
        self.assertIn(b'"model": "muse-voice-transcribe-1.0"', sent)
        # Nov 2 live 400: Meta requires this field; without it every
        # voice command and challenge video fails transcription.
        self.assertIn(b'"audioEncoding": "WAV"', sent)


class TestVerifiedMediaReplies(DbTestCase):
    def test_verified_photo_gets_already_verified(self):
        state.save_user("15550001111", name="Sagar Gharge",
                        address="999 Sample Road, Atlanta, GA 30309",
                        mailbox="999 Sample Road, Atlanta, GA 30309",
                        state="VERIFIED")
        user = state.get_user("15550001111")
        reply = main._handle_image("15550001111", user, "media123")
        self.assertIn("already verified", reply)
        self.assertNotIn("start by sending Hi", reply)
        self.assertEqual(state.get_user("15550001111")["state"], "VERIFIED")

    def test_verified_video_gets_already_verified(self):
        state.save_user("15550001111", name="Sagar Gharge",
                        address="999 Sample Road, Atlanta, GA 30309",
                        mailbox="999 Sample Road, Atlanta, GA 30309",
                        state="VERIFIED")
        user = state.get_user("15550001111")
        reply = main._handle_video("15550001111", user, "media123")
        self.assertIn("already verified", reply)
        self.assertNotIn("start by sending Hi", reply)
        self.assertEqual(state.get_user("15550001111")["state"], "VERIFIED")


class TestWebhookDedup(DbTestCase):
    def _payload(self, msg_id):
        return json.dumps({
            "entry": [{"changes": [{"value": {"messages": [
                {"from": PHONE1, "id": msg_id, "type": "text",
                 "text": {"body": "COUNT"}}]}}]}]}).encode()

    def _request(self, body):
        class Req:
            headers = {}

            async def body(self):
                return body

        return Req()

    def _verified_user(self):
        state.save_user(PHONE1, name="Sagar Gharge", address=ADDR,
                        mailbox=ADDR, state="VERIFIED")

    def test_same_message_id_answered_once(self):
        self._verified_user()
        with mock.patch.object(main.whatsapp, "send_text") as send_mock:
            asyncio.run(main.receive_webhook(
                self._request(self._payload("wamid.dedup1"))))
            asyncio.run(main.receive_webhook(
                self._request(self._payload("wamid.dedup1"))))
        self.assertEqual(send_mock.call_count, 1)

    def test_different_message_ids_each_answered(self):
        self._verified_user()
        with mock.patch.object(main.whatsapp, "send_text") as send_mock:
            asyncio.run(main.receive_webhook(
                self._request(self._payload("wamid.dedup2a"))))
            asyncio.run(main.receive_webhook(
                self._request(self._payload("wamid.dedup2b"))))
        self.assertEqual(send_mock.call_count, 2)


class TestAddressMatching(DbTestCase):
    def test_normalize_variants(self):
        canon = state.normalize_address(ADDR)
        for variant in [
            "999 Sample Road Atlanta GA 30309",
            "999 sample road, atlanta, ga 30309",
            "  999   Sample Road, Atlanta, GA 30309  ",
            "999 Sample Rd Atlanta Georgia 30309",
            "999 Sample Rd., Atlanta, GA 30309-1234",
        ]:
            self.assertEqual(state.normalize_address(variant), canon)
        self.assertNotEqual(
            state.normalize_address("998 Sample Road, Atlanta, GA 30309"),
            canon)
        self.assertNotEqual(
            state.normalize_address("999 Sample Road, Atlanta, GA 30308"),
            canon)

    def test_directional_merge(self):
        self.assertEqual(
            state.normalize_address("100 Peachtree St N.E. Atlanta GA 30303"),
            state.normalize_address(
                "100 Peachtree Street NE, Atlanta, GA 30303"))

    def test_members_of_variant(self):
        state.save_user(PHONE1, name="Sagar Gharge", address=ADDR,
                        mailbox=ADDR, state="VERIFIED")
        found = state.members_of("999 Sample Rd Atlanta GA 30309")
        self.assertEqual([u["phone"] for u in found], [PHONE1])

    def test_find_mailbox_variant(self):
        state.save_user(PHONE1, name="Sagar Gharge", address=ADDR,
                        mailbox=ADDR, state="VERIFIED")
        self.assertEqual(
            state.find_mailbox_by_address(
                "999 Sample Rd, Atlanta, Georgia 30309"), ADDR)

    def test_mail_upload_variant_notifies_and_stores_canonical(self):
        state.save_user(PHONE1, name="Sagar Gharge", address=ADDR,
                        mailbox=ADDR, state="VERIFIED")
        with mock.patch.object(config, "META_MODEL_API_BASE_URL", ""), \
                mock.patch.object(config, "META_MODEL_API_KEY", ""), \
                mock.patch.object(config, "FIREWORKS_API_KEY", ""), \
                mock.patch.object(config, "FIREWORKS_MODEL", ""), \
                mock.patch.object(main.whatsapp,
                                  "send_interactive_buttons") as btn_mock:
            resp = main.mail_upload(
                mailbox="999 Sample Rd Atlanta GA 30309",
                sender="ShopMart", subject="Deal",
                body_text="Big sale this weekend!")
        self.assertEqual(resp["notified"], 1)
        self.assertEqual(btn_mock.call_count, 1)
        # Stored under the household's canonical mailbox, so the
        # resident's MAIL digest finds it even though the business
        # typed a variant.
        mail = state.get_mail(ADDR)
        self.assertEqual(len(mail), 1)
        self.assertEqual(mail[0]["sender"], "ShopMart")


class TestOpenAndToday(DbTestCase):
    def _verified_user(self, phone=PHONE1, mailbox=ADDR):
        state.save_user(phone, name="Sagar Gharge", address=mailbox,
                        mailbox=mailbox, state="VERIFIED")
        return state.get_user(phone)

    def _add(self, sender, subject, body, created_at="now"):
        item = {"sender": sender, "subject": subject, "body": body,
                "category": "personal", "due": "—", "amount": "—",
                "scam": False}
        if created_at != "now":
            item["created_at"] = created_at
        return state.add_mail(ADDR, item)

    def test_open_full_letter(self):
        user = self._verified_user()
        mid = self._add("ShopMart", "Black Friday deals are here",
                        "Up to 50% off November 28-30.")
        reply = main._handle_text(PHONE1, user, f"OPEN {mid}")
        self.assertIn("ShopMart", reply)
        self.assertIn("Black Friday deals are here", reply)
        self.assertIn("Up to 50% off November 28-30.", reply)
        self.assertIn("Up to 50% off November 28-30.", main._handle_text(
            PHONE1, user, f"SHOW {mid}"))

    def test_open_refuses_other_mailbox_and_missing(self):
        user = self._verified_user()
        other = "1 Other Street, Atlanta, GA 30309"
        state.save_user(PHONE2, name="Priyanka Gharge", address=other,
                        mailbox=other, state="VERIFIED")
        oid = state.add_mail(other, {"sender": "First Demo Bank",
                                     "subject": "Offer", "body": "Hello",
                                     "category": "personal", "due": "—",
                                     "amount": "—", "scam": False})
        self.assertIn("not in your mailbox",
                      main._handle_text(PHONE1, user, f"OPEN {oid}"))
        self.assertIn("not in your mailbox",
                      main._handle_text(PHONE1, user, "OPEN 99999"))

    def test_open_button(self):
        user = self._verified_user()
        mid = self._add("ShopMart", "Deal", "Secret body text.")
        reply = main._handle_button(PHONE1, user, f"open_{mid}")
        self.assertIn("Secret body text.", reply)

    def test_today_filters(self):
        user = self._verified_user()
        now = datetime.now(timezone.utc).isoformat()
        yesterday = (datetime.now(timezone.utc)
                     - timedelta(days=1)).isoformat()
        today_id = self._add("ShopMart", "Today deal", "Today body.",
                             created_at=now)
        self._add("First Demo Bank", "Old offer", "Old body.",
                  created_at=yesterday)
        nodate_id = self._add("Barber Shop", "Coupon", "Coupon body.")
        with state._conn() as c:
            c.execute("UPDATE mail SET created_at=NULL WHERE id=?",
                      (nodate_id,))
        reply = main._handle_text(PHONE1, user, "TODAY")
        self.assertIn("Today deal", reply)
        self.assertIn("Today's mail: 1 letters", reply)
        self.assertNotIn("Old offer", reply)
        self.assertNotIn("Coupon", reply)
        self.assertIn("Reply OPEN <number> to read one fully", reply)

    def test_today_empty(self):
        user = self._verified_user()
        yesterday = (datetime.now(timezone.utc)
                     - timedelta(days=1)).isoformat()
        self._add("First Demo Bank", "Old offer", "Old body.",
                  created_at=yesterday)
        self.assertIn("No mail arrived today yet",
                      main._handle_text(PHONE1, user, "TODAY"))

    def test_voice_open_today_last(self):
        self.assertEqual(main._parse_voice_command("open letter two"),
                         "open 2")
        self.assertEqual(main._parse_voice_command("open mail 3"), "open 3")
        self.assertEqual(main._parse_voice_command("show me today's mail"),
                         "today")
        self.assertEqual(
            main._parse_voice_command("show me last mail from shopmart"),
            "latest 1 from shopmart")


class TestShredConfirmation(DbTestCase):
    def _setup(self):
        state.save_user(PHONE1, name="Sagar Gharge", address=ADDR,
                        mailbox=ADDR, state="VERIFIED")
        user = state.get_user(PHONE1)
        mid = state.add_mail(ADDR, {"sender": "ShopMart", "subject": "Deal",
                                    "body": "Sale!", "category": "personal",
                                    "due": "—", "amount": "—",
                                    "scam": False})
        return user, mid

    def test_shred_button_asks_first(self):
        user, mid = self._setup()
        reply = main._handle_button(PHONE1, user, f"shred_{mid}")
        self.assertEqual(reply[0], "buttons")
        self.assertIn("Shred letter", reply[1])
        self.assertIn("ShopMart", reply[1])
        self.assertEqual([b[0] for b in reply[2]],
                         [f"confirm_shred_{mid}", f"cancel_shred_{mid}"])
        # Nothing deleted yet.
        self.assertEqual(len(state.get_mail(ADDR)), 1)

    def test_confirm_shred_deletes(self):
        user, mid = self._setup()
        reply = main._handle_button(PHONE1, user, f"confirm_shred_{mid}")
        self.assertIn("Shredded & recycled", reply)
        self.assertEqual(len(state.get_mail(ADDR)), 0)
        self.assertEqual(state.sheets_saved(ADDR), 2)

    def test_cancel_shred_keeps(self):
        user, mid = self._setup()
        reply = main._handle_button(PHONE1, user, f"cancel_shred_{mid}")
        self.assertIn("Kept it", reply)
        self.assertEqual(len(state.get_mail(ADDR)), 1)

    def test_shred_button_invalid(self):
        user, _ = self._setup()
        reply = main._handle_button(PHONE1, user, "shred_99999")
        self.assertIsInstance(reply, str)
        self.assertIn("not in your mailbox", reply)

    def test_confirm_after_typed_shred(self):
        user, mid = self._setup()
        main._handle_text(PHONE1, user, f"SHRED {mid}")
        reply = main._handle_button(PHONE1, user, f"confirm_shred_{mid}")
        self.assertIn("not in your mailbox", reply)

    def test_webhook_sends_confirm_buttons(self):
        user, mid = self._setup()
        body = json.dumps({
            "entry": [{"changes": [{"value": {"messages": [
                {"from": PHONE1, "id": "wamid.btn1", "type": "interactive",
                 "interactive": {"button_reply": {"id": f"shred_{mid}"}}}]}}]}]}).encode()

        class Req:
            headers = {}

            async def body(self):
                return body

        with mock.patch.object(
                main.whatsapp, "send_interactive_buttons") as btn_mock, \
                mock.patch.object(main.whatsapp, "send_text") as txt_mock:
            asyncio.run(main.receive_webhook(Req()))
        self.assertEqual(btn_mock.call_count, 1)
        self.assertEqual(txt_mock.call_count, 0)
        self.assertIn("Shred letter", btn_mock.call_args[0][1])
        self.assertEqual([b[0] for b in btn_mock.call_args[0][2]],
                         [f"confirm_shred_{mid}", f"cancel_shred_{mid}"])
        self.assertEqual(len(state.get_mail(ADDR)), 1)


class TestNaturalMail(DbTestCase):
    def _setup(self):
        state.save_user(PHONE1, name="Sagar Gharge", address=ADDR,
                        mailbox=ADDR, state="VERIFIED")
        user = state.get_user(PHONE1)
        now = datetime.now(timezone.utc).isoformat()
        yesterday = (datetime.now(timezone.utc)
                     - timedelta(days=1)).isoformat()
        state.add_mail(ADDR, {"sender": "Barber Shop",
                              "subject": "$5 off haircut coupon",
                              "body": "Show this coupon for $5 off.",
                              "category": "personal", "due": "—",
                              "amount": "—", "scam": False,
                              "created_at": now})
        state.add_mail(ADDR, {"sender": "ShopMart",
                              "subject": "Deal one",
                              "body": "Sale one.", "category": "personal",
                              "due": "—", "amount": "—", "scam": False,
                              "created_at": now})
        state.add_mail(ADDR, {"sender": "ShopMart",
                              "subject": "Deal two",
                              "body": "Sale two.", "category": "personal",
                              "due": "—", "amount": "—", "scam": False,
                              "created_at": now})
        state.add_mail(ADDR, {"sender": "First Demo Bank",
                              "subject": "Statement ready",
                              "body": "Your bill is due soon.",
                              "category": "bill", "due": "Dec 5",
                              "amount": "$185.00", "scam": False,
                              "created_at": yesterday})
        return user

    def test_typed_natural_specific_today(self):
        user = self._setup()
        reply = main._handle_text(
            PHONE1, user, "I want to see today's Barber Shop email")
        self.assertIn("Show this coupon for $5 off.", reply)
        self.assertNotIn("Reply MAIL for today's digest", reply)

    def test_typed_natural_today_fallback(self):
        user = self._setup()
        with state._conn() as c:
            c.execute("UPDATE mail SET created_at=? WHERE sender=?",
                      ((datetime.now(timezone.utc) - timedelta(days=2))
                       .isoformat(), "Barber Shop"))
        reply = main._handle_text(
            PHONE1, user, "show me today's barber shop mail")
        self.assertIn("No Barber Shop mail arrived today", reply)
        self.assertIn("Show this coupon for $5 off.", reply)

    def test_typed_natural_multiple_lists(self):
        user = self._setup()
        reply = main._handle_text(PHONE1, user, "show me shopmart mail")
        self.assertIn("Deal one", reply)
        self.assertIn("Deal two", reply)
        self.assertIn("Reply OPEN <number>", reply)

    def test_typed_natural_keyword(self):
        user = self._setup()
        reply = main._handle_text(PHONE1, user, "show me the coupon")
        self.assertIn("$5 off haircut coupon", reply)

    def test_natural_unknown_not_help(self):
        user = self._setup()
        reply = main._handle_text(PHONE1, user, "show me the pizza menu")
        self.assertIn("No letters matching 'pizza menu'", reply)
        self.assertNotIn("Reply MAIL for today's digest", reply)

    def test_natural_no_hijack(self):
        user = self._setup()
        self.assertIn("MAIL — today's digest",
                      main._handle_text(PHONE1, user, "asdfgh"))
        self.assertIn("received", main._handle_text(PHONE1, user, "COUNT"))

    def test_voice_natural_specific(self):
        user = self._setup()
        with mock.patch.object(main.whatsapp, "download_media",
                               return_value=b"fake"), \
                mock.patch.object(main.voice_in_mod, "transcribe",
                                  return_value={
                                      "ok": True,
                                      "text": "show me today's barber shop mail"}):
            reply = main._handle_audio(PHONE1, user, "fake_media_id")
        self.assertIn("Show this coupon for $5 off.", reply)
        self.assertIn("🎙️ Heard:", reply)

    def test_voice_search_singular(self):
        user = self._setup()
        reply = main._voice_search(user, "bills")
        self.assertIn("Statement ready", reply)


class TestAttachmentsAndBackup(DbTestCase):
    def _verified_user(self):
        state.save_user(PHONE1, name="Sagar Gharge", address=ADDR,
                        mailbox=ADDR, state="VERIFIED")
        return state.get_user(PHONE1)

    def _letter(self, **extra):
        item = {"sender": "ShopMart", "subject": "Deal",
                "body": "Sale body.", "category": "personal",
                "due": "—", "amount": "—", "scam": False}
        item.update(extra)
        return state.add_mail(ADDR, item)

    def test_open_resends_local_attachment(self):
        user = self._verified_user()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        tmp.write(b"fake-jpeg")
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        mid = self._letter(attachment_media_id="old_mid",
                           attachment_mime="image/jpeg",
                           attachment_name="coupon.jpg",
                           attachment_path=tmp.name)
        with mock.patch.object(main.whatsapp, "upload_media",
                               return_value="new_mid") as up_mock, \
                mock.patch.object(main.whatsapp,
                                  "send_image_id") as img_mock:
            reply = main._handle_open(user, mid)
        up_mock.assert_called_once_with(tmp.name, "image/jpeg")
        self.assertEqual(img_mock.call_args[0][1], "new_mid")
        self.assertIn("Attachment", img_mock.call_args[1]["caption"])
        self.assertIn("Attachment sent above.", reply)
        self.assertNotIn("see the original message", reply)
        self.assertIn("Sale body.", reply)

    def test_open_falls_back_to_media_id(self):
        user = self._verified_user()
        mid = self._letter(attachment_media_id="old_mid",
                           attachment_mime="image/jpeg",
                           attachment_name="coupon.jpg")
        with mock.patch.object(main.whatsapp, "upload_media") as up_mock, \
                mock.patch.object(main.whatsapp,
                                  "send_image_id") as img_mock:
            reply = main._handle_open(user, mid)
        up_mock.assert_not_called()
        self.assertEqual(img_mock.call_args[0][1], "old_mid")
        self.assertIn("Attachment sent above.", reply)

    def test_open_attachment_failure_falls_back(self):
        user = self._verified_user()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        tmp.write(b"fake-jpeg")
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        mid = self._letter(attachment_media_id="old_mid",
                           attachment_mime="image/jpeg",
                           attachment_name="coupon.jpg",
                           attachment_path=tmp.name)
        with mock.patch.object(main.whatsapp, "upload_media",
                               side_effect=Exception("boom")), \
                mock.patch.object(main.whatsapp, "send_image_id") as img_mock:
            reply = main._handle_open(user, mid)
        img_mock.assert_not_called()
        self.assertIn("see the original message", reply)
        self.assertIn("Sale body.", reply)

    def test_upload_saves_attachment_locally(self):
        self._verified_user()
        att = mock.Mock()
        att.filename = "coupon.jpg"
        att.content_type = "image/jpeg"
        att.file = io.BytesIO(b"fake-bytes")
        with mock.patch.object(config, "META_MODEL_API_BASE_URL", ""), \
                mock.patch.object(config, "META_MODEL_API_KEY", ""), \
                mock.patch.object(config, "FIREWORKS_API_KEY", ""), \
                mock.patch.object(config, "FIREWORKS_MODEL", ""), \
                mock.patch.object(main.whatsapp, "upload_media",
                                  return_value="mid9"), \
                mock.patch.object(main.whatsapp, "send_interactive_buttons"):
            resp = main.mail_upload(mailbox=ADDR, sender="ShopMart",
                                    subject="Deal", body_text="Sale!",
                                    attachment=att)
        path = resp["attachment"]["attachment_path"]
        self.assertTrue(path and os.path.exists(path))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"fake-bytes")
        row = state.get_mail_by_id(resp["mail_id"])
        self.assertEqual(row["attachment_path"], path)
        self.assertEqual(resp["notified"], 1)

    def test_admin_backup(self):
        self._verified_user()
        self._letter()
        path = main._save_attachment("midX", "doc.pdf", b"pdf-bytes")
        self.assertTrue(path and os.path.exists(path))

        class Req:
            headers = {"X-Admin-Key": "testkey"}
            query_params = {}

        with mock.patch.object(config, "ADMIN_KEY", "testkey"):
            resp = main.admin_backup(Req())
            with self.assertRaises(main.HTTPException):
                class BadReq:
                    headers = {}
                    query_params = {}
                main.admin_backup(BadReq())
        import zipfile
        names = zipfile.ZipFile(io.BytesIO(resp.body)).namelist()
        self.assertIn("mailboxai.db", names)
        self.assertIn("attachments/midX_doc.pdf", names)


class TestConversational(DbTestCase):
    def _verified_user(self):
        state.save_user(PHONE1, name="Sagar Gharge", address=ADDR,
                        mailbox=ADDR, state="VERIFIED")
        return state.get_user(PHONE1)

    def test_thanks_not_a_search(self):
        user = self._verified_user()
        reply = main._handle_text(PHONE1, user, "thanks mailbox")
        self.assertIn("You're welcome", reply)
        self.assertNotIn("No letters matching", reply)
        self.assertNotIn("Reply MAIL for today's digest", reply)

    def test_who_are_you(self):
        user = self._verified_user()
        reply = main._handle_text(PHONE1, user, "who are you")
        self.assertIn("I'm MailboxAI", reply)
        self.assertIn("OPEN <number>", reply)
        self.assertIn(ADDR, reply)

    def test_who_are_you_before_onboarding(self):
        reply = main._handle_text(PHONE1, None, "who are you")
        self.assertIn("I'm MailboxAI", reply)
        self.assertIn("full name", reply)
        self.assertEqual(state.get_user(PHONE1)["state"], "AWAITING_NAME")

    def test_greeting_farewell_ack(self):
        user = self._verified_user()
        self.assertIn("Send MAIL", main._handle_text(PHONE1, user, "hi"))
        self.assertIn("Bye!", main._handle_text(PHONE1, user, "bye"))
        self.assertEqual(main._handle_text(PHONE1, user, "ok"), "👍 Got it.")

    def test_conversational_no_hijack(self):
        user = self._verified_user()
        self.assertIn("received", main._handle_text(PHONE1, user, "COUNT"))
        self.assertIn("Your mailbox is empty",
                      main._handle_text(PHONE1, user, "MAIL"))
        self.assertIn("today's Barber Shop mail",
                      main._handle_text(PHONE1, user, "help"))

    def test_voice_thanks_and_who(self):
        user = self._verified_user()
        for heard, want in (("thanks", "You're welcome"),
                            ("who are you", "I'm MailboxAI")):
            with mock.patch.object(main.whatsapp, "download_media",
                                   return_value=b"fake"), \
                    mock.patch.object(main.voice_in_mod, "transcribe",
                                      return_value={"ok": True,
                                                    "text": heard}):
                reply = main._handle_audio(PHONE1, user, "fake_media_id")
            self.assertIn(want, reply)
            self.assertIn("🎙️ Heard:", reply)


class TestWelcomeImage(DbTestCase):
    def test_first_hi_sends_picture_then_welcome(self):
        with mock.patch.object(main.whatsapp, "upload_media",
                               return_value="wmid") as up_mock, \
                mock.patch.object(main.whatsapp,
                                  "send_image_id") as img_mock:
            reply = main._handle_text(PHONE1, None, "Hi")
        up_mock.assert_called_once()
        self.assertTrue(up_mock.call_args[0][0].endswith(
            "mailbox-welcome.jpg"))
        self.assertEqual(up_mock.call_args[0][1], "image/jpeg")
        self.assertEqual(img_mock.call_args[0][1], "wmid")
        self.assertIn("contact photo", img_mock.call_args[1]["caption"])
        self.assertIn("Save Trees", img_mock.call_args[1]["caption"])
        self.assertIn("full name", reply)
        self.assertEqual(state.get_user(PHONE1)["state"], "AWAITING_NAME")

    def test_welcome_image_failure_still_welcomes(self):
        with mock.patch.object(main.whatsapp, "upload_media",
                               side_effect=Exception("boom")), \
                mock.patch.object(main.whatsapp, "send_image_id") as img_mock:
            reply = main._handle_text(PHONE1, None, "Hi")
        img_mock.assert_not_called()
        self.assertIn("full name", reply)

    def test_repeat_hi_sends_no_picture(self):
        state.save_user(PHONE1, name="Sagar Gharge", address=ADDR,
                        mailbox=ADDR, state="VERIFIED")
        user = state.get_user(PHONE1)
        with mock.patch.object(main.whatsapp, "upload_media") as up_mock:
            reply = main._handle_text(PHONE1, user, "hi")
        up_mock.assert_not_called()
        self.assertIn("Send MAIL", reply)


class TestAwaitingNameGreeting(DbTestCase):
    def _awaiting_name_user(self):
        state.save_user(PHONE1, state="AWAITING_NAME")
        return state.get_user(PHONE1)

    def test_hi_in_name_step_is_not_a_name(self):
        user = self._awaiting_name_user()
        reply = main._handle_text(PHONE1, user, "Hi")
        self.assertNotIn("Thanks, Hi", reply)
        self.assertIn("full name", reply)
        saved = state.get_user(PHONE1)
        self.assertEqual(saved["state"], "AWAITING_NAME")
        self.assertIsNone(saved.get("name"))

    def test_thanks_in_name_step_is_not_a_name(self):
        user = self._awaiting_name_user()
        reply = main._handle_text(PHONE1, user, "thanks")
        self.assertIn("full name", reply)
        saved = state.get_user(PHONE1)
        self.assertEqual(saved["state"], "AWAITING_NAME")
        self.assertIsNone(saved.get("name"))

    def test_real_name_still_accepted(self):
        user = self._awaiting_name_user()
        reply = main._handle_text(PHONE1, user, "Sagar Gharge")
        self.assertIn("Thanks, Sagar!", reply)
        saved = state.get_user(PHONE1)
        self.assertEqual(saved["state"], "AWAITING_LICENSE")
        self.assertEqual(saved["name"], "Sagar Gharge")


class TestRestyle(DbTestCase):
    def _verified_user(self):
        state.save_user(PHONE1, name="Sagar Gharge", address=ADDR,
                        mailbox=ADDR, state="VERIFIED")
        return state.get_user(PHONE1)

    def _letter(self, **extra):
        item = {"sender": "ShopMart", "subject": "Deal",
                "body": "Sale body.", "category": "personal",
                "due": "—", "amount": "—", "scam": False}
        item.update(extra)
        return state.add_mail(ADDR, item)

    def test_digest_card_is_a_card(self):
        items = [
            {"id": 1, "sender": "First Demo Bank", "subject": "Statement ready",
             "body": "", "category": "bill", "due": "November 15, 2026",
             "amount": "$412.30", "scam": False,
             "attachment_media_id": None},
            {"id": 2, "sender": "Barber Shop", "subject": "Coupon",
             "body": "", "category": "junk", "due": "—", "amount": "—",
             "scam": False, "attachment_media_id": "mid1"},
        ]
        card = digest_mod.build_digest_card(items, sheets_saved=4)
        self.assertIn("📬 *MailboxAI — today's mail: 2 letters*", card)
        self.assertIn("━━━━━━━━━━━━━━", card)
        self.assertIn("🧾 BILL — *First Demo Bank* — Statement ready", card)
        self.assertIn("November 15, 2026", card)
        self.assertIn("$412.30", card)
        self.assertIn("🗑️ JUNK — *Barber Shop* — Coupon 📎", card)
        self.assertIn("♻️ *Paper saved so far: 4 sheets", card)
        self.assertIn("Reply OPEN <number> to read one fully", card)

    def test_mail_line_icons_and_bold_sender(self):
        m = {"id": 7, "sender": "ShopMart", "subject": "Deal",
             "body": "", "category": "urgent", "due": "—", "amount": "—",
             "scam": True, "attachment_media_id": None,
             "created_at": "2026-11-03T10:00:00"}
        line = main._mail_line(m)
        self.assertIn("🚨 URGENT — *ShopMart* — Deal", line)
        self.assertIn("⚠ Possible scam", line)
        self.assertIn("(2026-11-03)", line)

    def test_help_is_a_menu_not_a_wall(self):
        menu = main._help_text()
        self.assertIn("📬 *MailboxAI — what you can say:*", menu)
        self.assertIn("• MAIL — today's digest", menu)
        self.assertIn("• OPEN <number> — read a letter fully", menu)
        self.assertIn("• SAVED — your paper total", menu)
        self.assertIn("today's Barber Shop mail", menu)

    def test_mail_returns_digest_buttons(self):
        user = self._verified_user()
        self._letter()
        reply = main._handle_text(PHONE1, user, "MAIL")
        self.assertEqual(reply[0], "buttons")
        self.assertIn("today's mail: 1 letters", reply[1])
        self.assertEqual([b[0] for b in reply[2]],
                         ["saved", "count", "help"])

    def test_digest_buttons_answer(self):
        user = self._verified_user()
        self._letter()
        self.assertIn("Paper saved:",
                      main._handle_button(PHONE1, user, "saved"))
        self.assertIn("received",
                      main._handle_button(PHONE1, user, "count"))
        self.assertIn("what you can say",
                      main._handle_button(PHONE1, user, "help"))

    def test_voice_mail_gets_card_text_not_buttons(self):
        user = self._verified_user()
        self._letter()
        with mock.patch.object(main.whatsapp, "download_media",
                               return_value=b"fake"), \
                mock.patch.object(main.voice_in_mod, "transcribe",
                                  return_value={"ok": True, "text": "mail"}):
            reply = main._handle_audio(PHONE1, user, "fake_media_id")
        self.assertIsInstance(reply, str)
        self.assertIn("🎙️ Heard:", reply)
        self.assertIn("today's mail: 1 letters", reply)

    def test_notification_note_is_a_card(self):
        state.save_user(PHONE1, name="Sagar Gharge", address=ADDR,
                        mailbox=ADDR, state="VERIFIED")
        with mock.patch.object(config, "META_MODEL_API_BASE_URL", ""), \
                mock.patch.object(config, "META_MODEL_API_KEY", ""), \
                mock.patch.object(config, "FIREWORKS_API_KEY", ""), \
                mock.patch.object(config, "FIREWORKS_MODEL", ""), \
                mock.patch.object(main.whatsapp,
                                  "send_interactive_buttons") as btn_mock:
            main.mail_upload(mailbox=ADDR, sender="ShopMart",
                             subject="Deal", body_text="Big sale!")
        note = btn_mock.call_args[0][1]
        self.assertIn("*New mail #", note)
        self.assertIn("*ShopMart*", note)
        self.assertIn("Deal", note)
        self.assertIn("Tap a button below", note)


if __name__ == "__main__":
    unittest.main()
