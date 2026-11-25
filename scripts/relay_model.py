"""Bounded Responses API adapter for Kakou's quota-limited credentials."""

import http.client
import json
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from time import monotonic

from message_protocol import ProtocolError, parse_json
from model_boundary import RATES, RELAY_MODEL, ModelError, ProviderReply, Usage
from model_credentials import read_key
from openai_model import system_prompt


RELAY_TARGET = "AndroidPluginAgent/Kakou"
HOST = "api.kakouai.com"
MAX_WIRE_BYTES = 128 * 1024
RATE = RATES["kakou"]
NANOS = 1_000_000_000


def money(value):
    if type(value) not in (int, float):
        raise ModelError("MODEL_USAGE_UNCERTAIN")
    try:
        result = Decimal(str(value)) * NANOS
        if not result.is_finite() or result < 0:
            raise ModelError("MODEL_USAGE_UNCERTAIN")
        return result
    except InvalidOperation:
        raise ModelError("MODEL_USAGE_UNCERTAIN") from None


def _exchange(path, payload, key, timeout):
    connection = http.client.HTTPSConnection(HOST, timeout=timeout)
    headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
    if key:
        headers["Authorization"] = "Bearer " + key
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        connection.request("POST" if body else "GET", path, body, headers)
        with connection.getresponse() as response:
            if response.status != 200:
                code = {401: "MODEL_AUTH_FAILED", 403: "MODEL_AUTH_FAILED", 429: "MODEL_RATE_LIMITED"}
                raise ModelError(code.get(response.status, "MODEL_UNAVAILABLE"))
            raw = response.read(MAX_WIRE_BYTES + 1)
        return parse_json(raw)
    except TimeoutError:
        raise ModelError("MODEL_TIMEOUT") from None
    except (OSError, http.client.HTTPException):
        raise ModelError("MODEL_UNAVAILABLE") from None
    except ProtocolError as error:
        raise ModelError("MODEL_OUTPUT_TOO_LARGE" if error.code == "PAYLOAD_TOO_LARGE" else "MODEL_INVALID_OUTPUT") from None
    finally:
        connection.close()


def _verify_pricing(catalog):
    entries = 0
    rate_keys = ("input_price", "cache_write_price", "cache_write_1h_price", "cache_write_12h_price", "cache_read_price")
    for group in catalog["data"]:
        for model in group["models"]:
            if model["name"] != RELAY_MODEL:
                continue
            entries += 1
            if (group["peak_rate_enabled"] is not False or not 0 < group["rate_multiplier"] <= 1
                    or model["budget_available"] is not True):
                raise ModelError("MODEL_CONFIG_INVALID")
            pricing = model["pricing"]
            if (pricing["billing_mode"] != "token"
                    or any(pricing.get(name) is not None for name in ("fast_multiplier", "flex_multiplier", "per_request_price"))):
                raise ModelError("MODEL_CONFIG_INVALID")
            for tier in [pricing, *pricing["intervals"]]:
                if (any(money(tier[key]) > RATE.input_nano_usd for key in rate_keys if tier.get(key) is not None)
                        or money(tier["input_price"]) > RATE.input_nano_usd
                        or money(tier["output_price"]) > RATE.output_nano_usd
                        or any(tier.get(name) is not None for name in (
                            "input_multiplier", "output_multiplier", "cache_write_multiplier", "cache_read_multiplier",
                            "per_request_price",
                        ))):
                    raise ModelError("MODEL_CONFIG_INVALID")
    if not entries:
        raise ModelError("MODEL_CONFIG_INVALID")


def _quota(report):
    quota = report["quota"]
    if report["mode"] != "quota_limited" or report["isValid"] is not True or quota["unit"] != "USD":
        raise ModelError("MODEL_CONFIG_INVALID")
    return money(quota["remaining"]), money(quota["used"])


def _content(response, key):
    if response.get("status") == "incomplete" and response.get("incomplete_details", {}).get("reason") == "max_output_tokens":
        raise ModelError("MODEL_OUTPUT_TOO_LARGE")
    if response.get("status") != "completed":
        raise ModelError("MODEL_INVALID_OUTPUT")
    messages = [item for item in response["output"] if item["type"] != "reasoning"]
    if len(messages) != 1 or messages[0]["type"] != "message" or messages[0]["role"] != "assistant":
        raise ModelError("MODEL_INVALID_OUTPUT")
    parts = messages[0]["content"]
    if len(parts) != 1 or parts[0]["type"] != "output_text":
        raise ModelError("MODEL_INVALID_OUTPUT")
    content = parts[0]["text"]
    if key in content or key in json.dumps(parse_json(content), ensure_ascii=False):
        raise ModelError("MODEL_INVALID_OUTPUT")
    return content


class RelayProvider:
    def __init__(self):
        self._key = read_key(RELAY_TARGET)

    def invoke(self, request, stop):
        deadline = monotonic() + request.timeout_seconds
        usage = billed = None
        exact_bill = False
        submitted = False

        def exchange(path, payload=None, *, authenticated=True):
            remaining = deadline - monotonic()
            if stop.is_set():
                raise ModelError("MODEL_CANCELLED")
            if remaining <= 0:
                raise ModelError("MODEL_TIMEOUT")
            return _exchange(path, payload, self._key if authenticated else None, remaining)

        try:
            if self._key in request.input_text:
                raise ModelError("MODEL_CONFIG_INVALID")
            _verify_pricing(exchange("/api/v1/pricing/public", authenticated=False))
            before_report = exchange("/v1/usage")
            remaining, before = _quota(before_report)
            if remaining < RATE.ceiling(request.output_tokens):
                raise ModelError("MODEL_BUDGET_EXHAUSTED")
            payload = {"model": RELAY_MODEL, "instructions": system_prompt(request), "input": request.input_text}
            count = exchange("/v1/responses/input_tokens", payload)["input_tokens"]
            if type(count) is not int or count < 1:
                raise ModelError("MODEL_CONFIG_INVALID")
            if count > RATE.max_input_tokens:
                raise ModelError("MODEL_INPUT_TOO_LARGE")
            if request.output_tokens < 16:
                raise ModelError("MODEL_CONFIG_INVALID")
            submitted = True
            response = exchange("/v1/responses", {
                **payload, "max_output_tokens": request.output_tokens, "store": False,
                "reasoning": {"effort": "low"}, "text": {"format": {"type": "json_object"}},
            })
            if response["model"] != RELAY_MODEL:
                raise ModelError("MODEL_USAGE_UNCERTAIN")
            used = response["usage"]
            usage = Usage(used["input_tokens"], used["output_tokens"], used["input_tokens_details"]["cached_tokens"])
            if (any(type(value) is not int for value in (usage.input_tokens, usage.output_tokens, usage.cached_input_tokens))
                    or not 0 <= usage.cached_input_tokens <= usage.input_tokens <= RATE.max_input_tokens
                    or not 0 <= usage.output_tokens <= request.output_tokens
                    or used["total_tokens"] != usage.input_tokens + usage.output_tokens):
                raise ModelError("MODEL_USAGE_UNCERTAIN")
            # A shared credential can have unrelated concurrent traffic. Settle by this reply's
            # own usage at the approved ceiling unless the quota movement can be reconciled.
            billed = RATE.cost(usage)
            before_stats = before_report["usage"]["total"]
            try:
                after = exchange("/v1/usage")
                _, quota_used = _quota(after)
                stats = after["usage"]["total"]
                token_delta = sum(stats[field] - before_stats[field] for field in (
                    "input_tokens", "cache_creation_tokens", "cache_read_tokens",
                ))
                delta = quota_used - before
                if (stats["requests"] - before_stats["requests"] == 1 and token_delta == usage.input_tokens
                        and stats["output_tokens"] - before_stats["output_tokens"] == usage.output_tokens
                        and 0 < delta <= billed):
                    billed = int(delta.to_integral_value(rounding=ROUND_CEILING))
                    exact_bill = True
            except (KeyError, TypeError, ValueError, ModelError):
                pass
            return ProviderReply(_content(response, self._key), usage, billed_nano_usd=billed, exact_bill=exact_bill)
        except ModelError as error:
            rejected = not submitted or error.code in {"MODEL_AUTH_FAILED", "MODEL_RATE_LIMITED"}
            return ProviderReply(None, Usage(0, 0) if rejected else usage, error.code,
                                 0 if rejected else billed, rejected or exact_bill)
        except (KeyError, TypeError, ValueError, ProtocolError):
            error = "MODEL_INVALID_OUTPUT" if submitted else "MODEL_CONFIG_INVALID"
            return ProviderReply(None, usage if submitted else Usage(0, 0), error,
                                 billed if submitted else 0, exact_bill if submitted else True)
