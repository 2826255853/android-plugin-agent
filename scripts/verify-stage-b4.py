"""Exercise the real inbox-to-draft path with fixed model responses."""

import hashlib
import json
import sqlite3
import unittest
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from dialogue_coordinator import DialogueCoordinator, envelope, timestamp
from dialogue_draft import DRAFT_PROMPT_VERSION, MOCK_NOTES_REQUEST
from dialogue_protocol import parse, state_of
from message_protocol import ProtocolError
from model_boundary import MAX_OUTPUT_BYTES, MAX_OUTPUT_TOKENS, PROMPT_VERSION, ProviderReply, Usage
from receiver_inbox import Inbox
from spec_workflow import (
    SPEC_DIRECTORY, SPEC_METADATA, SUPPORTED_CAPABILITIES, SpecError, load_json, review_spec, save_confirmed,
    spec_json, validate_spec,
)


PROJECT = "fixture-notes"
SAMPLE = load_json(SPEC_DIRECTORY / "examples" / "quick-notes-v1.json")
CANDIDATE = {key: value for key, value in SAMPLE.items() if key not in SPEC_METADATA}


class FixtureProvider:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def invoke(self, request, stop):
        self.requests.append(request)
        return ProviderReply(self.response, Usage(40, 30))


class StageB4Tests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory(prefix="stage-b4-")
        self.addCleanup(scratch.cleanup)
        self.path = Path(scratch.name) / "inbox.sqlite3"
        self.store = Path(scratch.name) / "projects"

    def ingest(self, coordinator, inbox, body):
        key = ("fixture-phone", PROJECT, str(uuid4()))
        message = {
            "deviceId": key[0], "projectId": key[1], "messageId": key[2], "createdAt": timestamp(),
            "body": body, "bodySha256": hashlib.sha256(body.encode()).hexdigest(), "sequence": 1,
            "messageType": "user.message",
        }
        row, _ = inbox.save(message, timestamp(), datetime.now(timezone.utc) - timedelta(days=1))
        coordinator.ingest(row)
        return key, row

    def page(self, coordinator, key):
        page = coordinator.pull(envelope(key, "dialogue.pull", after=0, limit=20))
        return parse(json.dumps(page, ensure_ascii=False))["events"]

    def round_trip(self, response, expected_state, *, body=MOCK_NOTES_REQUEST):
        provider = FixtureProvider(response)
        with closing(sqlite3.connect(":memory:")) as db:
            inbox = Inbox(db)
            coordinator = DialogueCoordinator(inbox, provider, spec_store=self.store)
            key, row = self.ingest(coordinator, inbox, body)
            coordinator._call(*coordinator._claim())
            coordinator.ingest(row)
            assert coordinator._claim() is None
            events = self.page(coordinator, key)
            assert [event["state"] for event in events[:2]] == ["queued", "running"]
            head = events[-1]
            self.assertEqual(state_of(head), expected_state)
            session = coordinator._session(key)
            self.assertEqual(session["state"], expected_state)
            call = db.execute("""
                SELECT prompt_version, status, charged_nano_usd, candidate_json, error_code
                FROM dialogue_model_calls
            """).fetchone()
            expected_status = "failed" if expected_state == "failed" else "completed"
            charge = 0 if head.get("code") == "MODEL_INPUT_TOO_LARGE" else 24_000
            self.assertEqual(tuple(call), (DRAFT_PROMPT_VERSION, expected_status, charge, None, head.get("code")))
            if expected_state != "result_ready":
                confirmation = envelope(key, "phone.confirm", eventId=str(uuid4()), sequence=1, createdAt=timestamp(),
                                        messageId=key[2], inReplyTo=head["eventId"],
                                        specVersion=head.get("specVersion", 1), specSha256=head.get("specSha256", "0" * 64))
                with self.assertRaisesRegex(ProtocolError, "invalid request") as error:
                    coordinator.submit(confirmation)
                self.assertEqual(error.exception.code, "INVALID_STATE")
            self.assertEqual(self.page(coordinator, key), events)
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE name = 'tasks'").fetchall(), [])
        return events, provider.requests

    def assert_spec(self, event, expected_version=1):
        original = event["specJson"]
        spec = json.loads(original)
        self.assertEqual(original, spec_json(spec))
        self.assertEqual(event["specSha256"], hashlib.sha256(original.encode()).hexdigest())
        self.assertEqual((spec["status"], spec["schemaVersion"], spec["projectId"], spec["version"]),
                         ("draft", 1, PROJECT, expected_version))
        self.assertEqual(event["specVersion"], expected_version)
        return spec

    def test_complete_spec_uses_desktop_identity_version_and_checks(self):
        saved = save_confirmed({**SAMPLE, "projectId": PROJECT}, self.store)
        before = saved.read_bytes()
        # Even plausible model-supplied metadata is replaced by desktop values.
        proposed = {**CANDIDATE, "projectId": "another-project", "version": 91, "status": "confirmed", "schemaVersion": 9}
        events, requests = self.round_trip(spec_json(proposed), "result_ready")
        self.assertEqual([event["type"] for event in events[2:]], ["desktop.review"])
        head = events[-1]
        spec = self.assert_spec(head, 2)
        validate_spec(spec, require_confirmable=True)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].output_tokens, MAX_OUTPUT_TOKENS)
        self.assertEqual(json.loads(requests[0].input_text), {
            "userMessage": MOCK_NOTES_REQUEST,
            "project": {"projectId": PROJECT, "nextSpecVersion": 2},
            "hostCapabilities": SUPPORTED_CAPABILITIES,
        })
        self.assertEqual(list(saved.parent.iterdir()), [saved])
        self.assertEqual(saved.read_bytes(), before)

    def test_missing_decisions_are_stage21_questions_bound_to_draft(self):
        proposed = {**CANDIDATE, "goal": " ", "pages": [], "workflows": [], "storesData": None,
                    "collections": [], "acceptanceCriteria": []}
        events, _ = self.round_trip(spec_json(proposed), "waiting_for_user")
        draft, question = events[2:]
        self.assertEqual((draft["type"], question["type"]), ("desktop.draft", "desktop.question"))
        spec = self.assert_spec(draft)
        self.assertEqual(question["specSha256"], draft["specSha256"])
        self.assertEqual(question["specVersion"], draft["specVersion"])
        expected = review_spec(spec, self.store)["questions"]
        self.assertEqual(question["question"].splitlines(), [item["question"] for item in expected])
        with self.assertRaises(SpecError):
            validate_spec(spec, require_confirmable=True)

    def test_unknown_capability_requires_explanation_before_review(self):
        requested = {"capability": "device.contacts", "justification": "选取联系人。"}
        proposed = {**CANDIDATE, "permissions": [*CANDIDATE["permissions"], requested]}
        events, _ = self.round_trip(spec_json(proposed), "waiting_for_user")
        spec = self.assert_spec(events[2])
        with self.assertRaisesRegex(SpecError, "reason and alternative"):
            validate_spec(spec, require_confirmable=True)
        self.assertIn("替代方案", events[-1]["question"])
        explanation = {"request": requested["capability"], "reason": "宿主未开放通讯录。", "alternative": "手动输入名称。"}
        events, _ = self.round_trip(spec_json({**proposed, "unsupportedItems": [explanation]}), "result_ready")
        spec = self.assert_spec(events[-1])
        validate_spec(spec, require_confirmable=True)
        self.assertEqual(spec["unsupportedItems"], [explanation])
        self.assertIn("1 项请求超出宿主能力", events[-1]["summary"])

    def test_invalid_structure_does_not_publish_any_draft(self):
        bad_field = deepcopy(CANDIDATE)
        bad_field["collections"][0]["fields"][0]["maxLength"] = True
        duplicate = deepcopy(CANDIDATE)
        duplicate["collections"][0]["fields"].append(duplicate["collections"][0]["fields"][0])
        missing = dict(CANDIDATE)
        del missing["goal"]
        for name, response in {
            "invalid JSON": "{",
            "duplicate key": '{"goal":0,"goal":1}',
            "array root": "[]",
            "invalid Unicode": '{"goal":"\\ud800"}',
            "nested unknown field": spec_json({**CANDIDATE, "pages": [{**CANDIDATE["pages"][0], "command": "fixture"}]}),
            "model validation claim": spec_json({**CANDIDATE, "verified": True}),
            "model revision metadata": spec_json({**CANDIDATE, "revision": {"baseVersion": 1}}),
            "missing key": spec_json(missing),
            "invalid optional field": spec_json(bad_field),
            "duplicate id": spec_json(duplicate),
            "goal exceeds schema limit": spec_json({**CANDIDATE, "goal": "x" * 2001}),
        }.items():
            with self.subTest(name=name):
                events, _ = self.round_trip(response, "failed")
                self.assertEqual([(event["type"], event["code"]) for event in events[2:]],
                                 [("desktop.error", "MODEL_INVALID_OUTPUT")])

    def test_byte_limits_apply_before_parsing_or_invocation(self):
        long_reply = '{"irrelevant":"' + "记" * (MAX_OUTPUT_BYTES // 3) + '"}'
        events, requests = self.round_trip(long_reply, "failed")
        self.assertEqual(events[-1]["code"], "MODEL_OUTPUT_TOO_LARGE")
        self.assertEqual(len(requests), 1)
        events, requests = self.round_trip(spec_json(CANDIDATE), "failed", body="记" * 12_000)
        self.assertEqual(events[-1]["code"], "MODEL_INPUT_TOO_LARGE")
        self.assertEqual(requests, [])

    def test_legacy_candidates_are_checked_once_without_calling_provider(self):
        provider = FixtureProvider(None)
        with closing(sqlite3.connect(self.path)) as db:
            inbox = Inbox(db)
            coordinator = DialogueCoordinator(inbox, provider, spec_store=self.store)
            key, _ = self.ingest(coordinator, inbox, MOCK_NOTES_REQUEST)
            coordinator._claim()
            with db:
                db.execute("""
                    UPDATE dialogue_model_calls SET status = 'completed', prompt_version = ?, candidate_json = ?,
                        charged_nano_usd = 10800
                """, (PROMPT_VERSION, '{"goal":"B.3 incomplete fixture","storesData":true}'))
        with closing(sqlite3.connect(self.path)) as db:
            inbox = Inbox(db)
            recovered = DialogueCoordinator(inbox, provider, spec_store=self.store)
            history = self.page(recovered, key)
            self.assertEqual(history[-1]["code"], "MODEL_INVALID_OUTPUT")
            restarted = DialogueCoordinator(inbox, provider, spec_store=self.store)
            self.assertEqual(self.page(restarted, key), history)
            self.assertIsNone(restarted._claim())
            call = db.execute("SELECT candidate_json, charged_nano_usd FROM dialogue_model_calls").fetchone()
            self.assertEqual(tuple(call), (None, 10800))
        self.assertEqual(provider.requests, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
