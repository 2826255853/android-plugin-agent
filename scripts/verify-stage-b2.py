"""Repeatable model boundary and adapter checks with synthetic inputs only."""

import json
import os
import unittest
from dataclasses import asdict, replace
from threading import Event, Thread
from time import monotonic
from unittest.mock import patch
from uuid import uuid4

import model_credentials
from model_boundary import (
    MAX_INPUT_BYTES, MAX_OUTPUT_BYTES, MOCK_MODEL, OPENAI_MODEL, RELAY_MODEL, Config, ModelBoundary, ModelError,
    MockProvider, ProviderReply, Request, Usage,
)
from openai_model import MAX_RESPONSE_BYTES, OpenAIProvider, invoke_openai
from relay_model import RelayProvider


FICTIONAL_KEY = "sk-fixture-DO-NOT-USE-" + "X" * 20
SYNTHETIC_INPUT = "Private synthetic requirements for quick notes"


def request(**options):
    return replace(Request(uuid4(), "quick-notes", uuid4(), SYNTHETIC_INPUT), **options)


class ProviderStub:
    def __init__(self, invoke):
        self.invoke = invoke


class ModelChecks(unittest.TestCase):
    def setUp(self):
        self.config = Config("mock", MOCK_MODEL, 25_000_000)

    def verify_error(self, expected, action):
        with self.assertRaises(ModelError) as error:
            action()
        self.assertEqual(error.exception.code, expected)

    def test_simulated_output_and_exact_settlement(self):
        boundary = ModelBoundary(self.config, MockProvider())
        query = request()
        result = boundary.call(query)
        self.assertEqual(result.candidate, {"goal": "Keep short notes", "storesData": True})
        self.assertEqual(result.receipt.usage, Usage(24, 12))
        self.assertEqual(result.receipt.reserved_nano_usd, 19_353_600)
        self.assertEqual(result.receipt.charged_nano_usd, 10_800)
        self.assertTrue(result.receipt.exact_charge)
        self.assertEqual(boundary.accounting(query.project_id), (10_800, 0))
        self.assertEqual(boundary.receipt(query.request_id), result.receipt)
        self.assertNotIn(SYNTHETIC_INPUT, repr(query))
        self.assertNotIn("Keep short notes", repr(result))
        self.verify_error("MODEL_REQUEST_CONFLICT", lambda: boundary.call(query))

    def test_preflight_budget_input_and_configuration(self):
        query = request()
        provider = ProviderStub(lambda *args: self.fail("preflight should stop invocation"))
        boundary = ModelBoundary(Config("mock", MOCK_MODEL, 1), provider)
        self.verify_error("MODEL_BUDGET_EXHAUSTED", lambda: boundary.call(query))
        self.verify_error("MODEL_INPUT_TOO_LARGE", lambda: boundary.call(replace(query, input_text="x" * (MAX_INPUT_BYTES + 1))))
        for invalid in (replace(query, timeout_seconds=float("inf")), replace(query, output_tokens=1025),
                        replace(query, prompt_version="arbitrary"), replace(query, version=True)):
            self.verify_error("MODEL_CONFIG_INVALID", lambda: boundary.call(invalid))
        self.verify_error("MODEL_CONFIG_INVALID", lambda: Config("openai", "gpt-4o-mini", 1))
        cancelled = Event()
        cancelled.set()
        self.verify_error("MODEL_CANCELLED", lambda: boundary.call(query, cancelled))
        self.assertEqual(boundary.accounting(query.project_id), (0, 0))

    def test_rounds_and_usage_uncertain_stop(self):
        boundary = ModelBoundary(self.config, MockProvider())
        first = request()
        for _ in range(5):
            boundary.call(replace(first, request_id=uuid4()))
        self.verify_error("MODEL_ROUND_LIMIT", lambda: boundary.call(first))
        boundary.call(request())
        for usage in (None, Usage(128001, 1), Usage(1, 257), Usage(1, 1, 2), Usage(True, 1)):
            with self.subTest(usage=usage):
                unknown = ModelBoundary(self.config, ProviderStub(lambda *_: ProviderReply("{}", usage)))
                response = unknown.call(first).receipt
                self.assertEqual(response.error, "MODEL_USAGE_UNCERTAIN")
                self.assertEqual(response.charged_nano_usd, response.reserved_nano_usd)
                self.assertFalse(response.exact_charge)
                self.verify_error("MODEL_BUDGET_EXHAUSTED", lambda: unknown.call(request()))

    def test_invalid_output_still_charged(self):
        for output, code in [
            ("[1]", "MODEL_INVALID_OUTPUT"), ('{"k":1,"k":1}', "MODEL_INVALID_OUTPUT"),
            ('{"k":1e9999}', "MODEL_INVALID_OUTPUT"), ('{"k":"\\ud800"}', "MODEL_INVALID_OUTPUT"),
            ('{"k":"' + "x" * MAX_OUTPUT_BYTES + '"}', "MODEL_OUTPUT_TOO_LARGE"),
        ]:
            with self.subTest(code=code):
                boundary = ModelBoundary(self.config, ProviderStub(lambda *_: ProviderReply(output, Usage(3, 2, 1))))
                reply = boundary.call(request())
                self.assertIsNone(reply.candidate)
                self.assertEqual(reply.receipt.error, code)
                self.assertEqual(reply.receipt.charged_nano_usd, 1575)

    def test_timeout_keeps_reservation_and_discards_late_output(self):
        stopped, released = Event(), Event()

        def late(_, stop):
            if stop.wait(2):
                stopped.set()
            released.wait(2)
            return ProviderReply("{}", Usage(3, 2))

        boundary = ModelBoundary(self.config, ProviderStub(late))
        query = request(timeout_seconds=0.1)
        try:
            self.verify_error("MODEL_TIMEOUT", lambda: boundary.call(query))
            self.assertTrue(stopped.wait(2))
            self.assertEqual(boundary.accounting(query.project_id), (0, 19_353_600))
            self.verify_error("MODEL_BUSY", lambda: boundary.call(request()))
        finally:
            released.set()
        deadline = monotonic() + 2
        while boundary.receipt(query.request_id) is None and monotonic() < deadline:
            Event().wait(0.01)
        receipt = boundary.receipt(query.request_id)
        self.assertEqual(receipt.error, "MODEL_TIMEOUT")
        self.assertTrue(receipt.exact_charge)
        self.assertIsNone(receipt.content_sha256)
        self.assertEqual(boundary.accounting(query.project_id), (1650, 0))

    def test_active_cancel_and_concurrency_limit(self):
        first_started, second_started, release, cancel = Event(), Event(), Event(), Event()

        def work(query, stop):
            if query.project_id == "quick-notes":
                first_started.set()
                stop.wait(2)
                return ProviderReply(None, None, "MODEL_CANCELLED")
            second_started.set()
            release.wait(2)
            return ProviderReply("{}", Usage(5, 5))

        boundary = ModelBoundary(self.config, ProviderStub(work))
        first, second = request(), request(project_id="second-project")
        exits = []

        def call(query, signal=None):
            try:
                exits.append(boundary.call(query, signal).receipt.error)
            except ModelError as error:
                exits.append(error.code)

        workers = [Thread(target=call, args=(first, cancel)), Thread(target=call, args=(second,))]
        for worker in workers:
            worker.start()
        try:
            self.assertTrue(first_started.wait(2) and second_started.wait(2))
            self.verify_error("MODEL_BUSY", lambda: boundary.call(request(project_id="third-project")))
        finally:
            cancel.set()
            release.set()
            for worker in workers:
                worker.join(3)
        self.assertCountEqual(exits, ["MODEL_CANCELLED", None])

    def test_provider_exception_redacted(self):
        def broken(*_):
            raise RuntimeError(FICTIONAL_KEY + SYNTHETIC_INPUT)

        result = ModelBoundary(self.config, ProviderStub(broken)).call(request())
        log = json.dumps(asdict(result.receipt), default=str)
        self.assertIn("MODEL_UNAVAILABLE", log)
        self.assertNotIn(FICTIONAL_KEY, log)
        self.assertNotIn(SYNTHETIC_INPUT, log)

    def test_openai_request_and_response_mapping(self):
        def post(raw, key, timeout, stop):
            body = json.loads(raw)
            self.assertEqual(key, FICTIONAL_KEY)
            self.assertNotIn(FICTIONAL_KEY.encode(), raw)
            self.assertEqual(body["model"], OPENAI_MODEL)
            self.assertEqual(body["max_completion_tokens"], 256)
            self.assertEqual(body["n"], 1)
            self.assertIs(body["store"], False)
            self.assertEqual(body["service_tier"], "default")
            self.assertNotIn("tools", body)
            self.assertNotIn("functions", body)
            self.assertEqual(timeout, 15)
            self.assertFalse(stop.is_set())
            return 200, self.provider_fixture('{"goal":"Fixture"}')

        with patch("openai_model.read_openai_key", return_value=FICTIONAL_KEY), patch("openai_model._post", post):
            boundary = ModelBoundary(Config("openai", OPENAI_MODEL, 25_000_000), OpenAIProvider())
            result = boundary.call(request())
        self.assertIsNone(result.receipt.error)
        self.assertEqual(result.candidate, {"goal": "Fixture"})
        self.assertEqual(result.receipt.usage, Usage(14, 3, 4))
        self.assertEqual(result.receipt.charged_nano_usd, 3600)

    def test_openai_errors_output_limits_and_secret_echo(self):
        escaped = '{"goal":"\\u0073' + FICTIONAL_KEY[1:] + '"}'
        for status, raw, expected in (
            (401, FICTIONAL_KEY.encode(), "MODEL_AUTH_FAILED"),
            (429, FICTIONAL_KEY.encode(), "MODEL_RATE_LIMITED"),
            (503, FICTIONAL_KEY.encode(), "MODEL_UNAVAILABLE"),
            (302, FICTIONAL_KEY.encode(), "MODEL_UNAVAILABLE"),
            (200, b"x" * (MAX_RESPONSE_BYTES + 1), "MODEL_OUTPUT_TOO_LARGE"),
            (200, self.provider_fixture(escaped), "MODEL_INVALID_OUTPUT"),
            (200, self.provider_fixture("{}", finish="length"), "MODEL_OUTPUT_TOO_LARGE"),
            (200, self.provider_fixture("{}", message_fields={"tool_calls": [{}]}), "MODEL_INVALID_OUTPUT"),
            (200, self.provider_fixture("{}", service_tier="priority"), "MODEL_USAGE_UNCERTAIN"),
        ):
            with self.subTest(expected=expected):
                result = invoke_openai(request(), Event(), FICTIONAL_KEY, lambda *_: (status, raw))
                self.assertEqual(result.error, expected)
                self.assertIsNone(result.content)
                self.assertNotIn(FICTIONAL_KEY, repr(result))

    def test_relay_quota_pricing_token_preflight_and_settlement(self):
        query = request(output_tokens=512)
        catalog = {"data": [{"peak_rate_enabled": False, "rate_multiplier": 0.35, "models": [{
            "name": RELAY_MODEL, "budget_available": True, "pricing": {
                "billing_mode": "token", "input_price": 0.000002, "output_price": 0.000010, "intervals": [],
            },
        }]}]}
        before = {
            "mode": "quota_limited", "isValid": True, "quota": {"remaining": 100, "used": 0, "unit": "USD"},
            "usage": {"total": {"input_tokens": 0, "cache_creation_tokens": 0, "cache_read_tokens": 0,
                                "output_tokens": 0, "requests": 0}},
        }
        response = {
            "model": RELAY_MODEL, "status": "completed",
            "usage": {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20,
                      "input_tokens_details": {"cached_tokens": 0}},
            "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "{}"}]}],
        }
        after = {**before, "quota": {**before["quota"], "used": 0.00004}, "usage": {"total": {
            **before["usage"]["total"], "input_tokens": 12, "output_tokens": 8, "requests": 1,
        }}}
        for mode, count, audit, expected, charge, exact in (
            ("unrestricted", 12, after, "MODEL_CONFIG_INVALID", 0, True),
            ("quota_limited", 4097, after, "MODEL_INPUT_TOO_LARGE", 0, True),
            ("quota_limited", 12, before, None, 240_000, False),
            ("quota_limited", 12, after, None, 40_000, True),
        ):
            endpoints = []

            def exchange(path, payload, key, timeout):
                endpoints.append(path)
                if path == "/api/v1/pricing/public":
                    self.assertIsNone(key)
                    return catalog
                self.assertEqual(key, FICTIONAL_KEY)
                if path == "/v1/usage":
                    return {**before, "mode": mode} if endpoints.count(path) == 1 else audit
                if path == "/v1/responses/input_tokens":
                    return {"input_tokens": count}
                self.assertEqual(path, "/v1/responses")
                self.assertEqual(payload["max_output_tokens"], 512)
                self.assertIs(payload["store"], False)
                self.assertNotIn("tools", payload)
                return response

            with self.subTest(mode=mode, count=count, charge=charge), \
                    patch("relay_model.read_key", return_value=FICTIONAL_KEY), patch("relay_model._exchange", exchange):
                boundary = ModelBoundary(Config("kakou", RELAY_MODEL, 50_000_000), RelayProvider())
                result = boundary.call(query)
                self.assertEqual(result.receipt.error, expected)
                self.assertEqual(result.receipt.charged_nano_usd, charge)
                self.assertEqual(result.receipt.exact_charge, exact)
                self.assertEqual("/v1/responses" in endpoints, expected is None)
                self.assertNotIn(FICTIONAL_KEY, repr(result))

    @unittest.skipUnless(os.name == "nt", "Windows Credential Manager required")
    def test_windows_credential_round_trip_and_cleanup(self):
        target = "AndroidPluginAgent/Test/" + str(uuid4())
        with patch.object(model_credentials, "TARGET", target):
            self.verify_error("MODEL_CONFIG_INVALID", model_credentials.read_openai_key)
            try:
                model_credentials.save_openai_key(FICTIONAL_KEY)
                self.assertTrue(model_credentials.read_openai_key() == FICTIONAL_KEY)
            finally:
                model_credentials._windows().CredDeleteW(target, 1, 0)
            self.verify_error("MODEL_CONFIG_INVALID", model_credentials.read_openai_key)

    @staticmethod
    def provider_fixture(content, *, finish="stop", message_fields=None, **fields):
        return json.dumps({
            "model": OPENAI_MODEL, "service_tier": "default",
            "choices": [{"finish_reason": finish, "message": {"role": "assistant", "content": content, **(message_fields or {})}}],
            "usage": {"prompt_tokens": 14, "completion_tokens": 3, "total_tokens": 17,
                      "prompt_tokens_details": {"cached_tokens": 4}},
            **fields,
        }).encode()


if __name__ == "__main__":
    unittest.main(verbosity=2)
