"""Provider-neutral Stage B model invocation and bounded accounting."""

from dataclasses import dataclass, field, replace
from hashlib import sha256
from json import dumps
from math import isfinite
from threading import Event, Lock, Thread
from time import monotonic
from typing import Protocol
from uuid import UUID

from dialogue_draft import DRAFT_PROMPT_VERSION, MOCK_NOTES_REQUEST, mock_draft
from dialogue_protocol import strict_object
from message_protocol import PROJECT_ID, ProtocolError


PROMPT_VERSION = "spec-draft-v1"
MOCK_MODEL = "fixture-v1"
OPENAI_MODEL = "gpt-4o-mini-2024-07-18"
RELAY_MODEL = "gpt-6.1-sol"
DEVELOPMENT_PROMPT_VERSION = "plugin-development-v1"
MAX_INPUT_BYTES = 24 * 1024
MAX_OUTPUT_BYTES = 12 * 1024
MAX_OUTPUT_TOKENS = 1024
MAX_CLARIFICATIONS = 4
MAX_CONCURRENCY = 2
MAX_PROJECT_BUDGET = 100_000_000  # USD 0.10 in nano-USD
MODELS = {"mock": MOCK_MODEL, "openai": OPENAI_MODEL, "kakou": RELAY_MODEL}


class ModelError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Config:
    provider: str
    model: str
    budget_nano_usd: int

    def __post_init__(self):
        if (not isinstance(self.provider, str) or self.provider not in MODELS
                or self.model != MODELS[self.provider] or type(self.budget_nano_usd) is not int
                or not 1 <= self.budget_nano_usd <= MAX_PROJECT_BUDGET):
            raise ModelError("MODEL_CONFIG_INVALID")


@dataclass(frozen=True)
class Request:
    request_id: UUID
    project_id: str
    session_id: UUID
    input_text: str = field(repr=False)
    timeout_seconds: float = 15
    output_tokens: int = 256
    version: int = 1
    prompt_version: str = PROMPT_VERSION


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0


@dataclass(frozen=True)
class Rate:
    max_input_tokens: int
    input_nano_usd: int
    cached_nano_usd: int
    output_nano_usd: int

    def ceiling(self, output_tokens: int) -> int:
        return self.cost(Usage(self.max_input_tokens, output_tokens))

    def cost(self, usage: Usage) -> int:
        return ((usage.input_tokens - usage.cached_input_tokens) * self.input_nano_usd
                + usage.cached_input_tokens * self.cached_nano_usd + usage.output_tokens * self.output_nano_usd)


@dataclass(frozen=True)
class ProviderReply:
    content: str | None = field(repr=False)
    usage: Usage | None
    error: str | None = None
    billed_nano_usd: int | None = None
    exact_bill: bool = True


class Provider(Protocol):
    def invoke(self, request: Request, stop: Event) -> ProviderReply: ...


@dataclass(frozen=True)
class Response:
    request_id: UUID
    model: str
    prompt_version: str
    reserved_nano_usd: int
    charged_nano_usd: int
    usage: Usage | None
    exact_charge: bool
    elapsed_ms: int
    content_sha256: str | None = None
    error: str | None = None
    usage_uncertain: bool = False
    version: int = 1


@dataclass(frozen=True)
class Result:
    receipt: Response
    candidate: dict | None = field(repr=False)


RATES = {
    "mock": Rate(128_000, 150, 75, 600),
    "openai": Rate(128_000, 150, 75, 600),
    "kakou": Rate(4096, 10_000, 10_000, 15_000),
}
PROVIDER_ERRORS = {
    "MODEL_TIMEOUT", "MODEL_CANCELLED", "MODEL_UNAVAILABLE", "MODEL_AUTH_FAILED", "MODEL_RATE_LIMITED",
    "MODEL_INVALID_OUTPUT", "MODEL_OUTPUT_TOO_LARGE", "MODEL_USAGE_UNCERTAIN",
    "MODEL_CONFIG_INVALID", "MODEL_INPUT_TOO_LARGE", "MODEL_BUDGET_EXHAUSTED",
}


class ModelBoundary:
    def __init__(self, config: Config, provider: Provider):
        self.config = config
        self.provider = provider
        self.rate = RATES[config.provider]
        self._lock = Lock()
        self._spent: dict[str, int] = {}
        self._reserved: dict[str, int] = {}
        self._attempts: dict[tuple[str, UUID], int] = {}
        self._receipts: dict[UUID, Response | None] = {}
        self._active: set[str] = set()

    def accounting(self, project_id: str) -> tuple[int, int]:
        with self._lock:
            return self._spent.get(project_id, 0), self._reserved.get(project_id, 0)

    def receipt(self, request_id: UUID) -> Response | None:
        with self._lock:
            return self._receipts.get(request_id)

    def has_request(self, request_id: UUID) -> bool:
        with self._lock:
            return request_id in self._receipts

    def call(self, request: Request, cancellation: Event | None = None, *, pre_reserved: bool = False) -> Result:
        """Use the in-memory ledger unless the receiver has already reserved the call in SQLite."""
        cancellation = cancellation if cancellation is not None else Event()
        self._validate(request)
        if cancellation.is_set():
            raise ModelError("MODEL_CANCELLED")
        amount = self.rate.ceiling(request.output_tokens)
        project = request.project_id
        session = (project, request.session_id)
        stop, done = Event(), Event()
        started = monotonic()
        result = None
        abort_code = None
        with self._lock:
            if not pre_reserved and request.request_id in self._receipts:
                raise ModelError("MODEL_REQUEST_CONFLICT")
            if project in self._active or len(self._active) >= MAX_CONCURRENCY:
                raise ModelError("MODEL_BUSY")
            if not pre_reserved:
                attempts = self._attempts.get(session, 0)
                if attempts >= 1 + MAX_CLARIFICATIONS:
                    raise ModelError("MODEL_ROUND_LIMIT")
                if self._spent.get(project, 0) + amount > self.config.budget_nano_usd:
                    raise ModelError("MODEL_BUDGET_EXHAUSTED")
                self._reserved[project] = amount
                self._attempts[session] = attempts + 1
                self._receipts[request.request_id] = None
            self._active.add(project)

        def run():
            nonlocal result
            try:
                reply = (ProviderReply(None, Usage(0, 0), "MODEL_CANCELLED", 0)
                         if stop.is_set() else self.provider.invoke(request, stop))
                result = self._settle(request, reply, amount, started)
            except Exception:
                result = self._settle(request, ProviderReply(None, None, "MODEL_UNAVAILABLE"), amount, started)
            with self._lock:
                if abort_code:
                    result = Result(replace(result.receipt, error=abort_code, content_sha256=None), None)
                if not pre_reserved:
                    self._spent[project] = self._spent.get(project, 0) + result.receipt.charged_nano_usd
                    del self._reserved[project]
                    self._receipts[request.request_id] = result.receipt
                self._active.remove(project)
                done.set()

        try:
            Thread(target=run, daemon=True).start()
        except RuntimeError:
            with self._lock:
                if not pre_reserved:
                    del self._reserved[project]
                    del self._receipts[request.request_id]
                    self._attempts[session] = attempts
                self._active.remove(project)
            raise ModelError("MODEL_UNAVAILABLE") from None

        while True:
            remaining = request.timeout_seconds - (monotonic() - started)
            if cancellation.is_set() or remaining <= 0:
                with self._lock:
                    if done.is_set():
                        return result
                    abort_code = "MODEL_CANCELLED" if cancellation.is_set() else "MODEL_TIMEOUT"
                    stop.set()
                if pre_reserved:
                    return self._settle(request, ProviderReply(None, None, abort_code), amount, started)
                raise ModelError(abort_code)
            if done.wait(min(0.025, remaining)):
                return result

    @staticmethod
    def _validate(request: Request):
        if (type(request.version) is not int or request.version != 1
                or request.prompt_version not in (PROMPT_VERSION, DRAFT_PROMPT_VERSION, DEVELOPMENT_PROMPT_VERSION)
                or not isinstance(request.request_id, UUID) or not isinstance(request.session_id, UUID)
                or not isinstance(request.project_id, str) or not PROJECT_ID.fullmatch(request.project_id)
                or type(request.timeout_seconds) not in (int, float)
                or not isfinite(request.timeout_seconds) or not 0 < request.timeout_seconds <= 30
                or type(request.output_tokens) is not int or not 1 <= request.output_tokens <= MAX_OUTPUT_TOKENS
                or not isinstance(request.input_text, str) or not request.input_text.strip()):
            raise ModelError("MODEL_CONFIG_INVALID")
        try:
            if len(request.input_text.encode("utf-8")) > MAX_INPUT_BYTES:
                raise ModelError("MODEL_INPUT_TOO_LARGE")
        except UnicodeError:
            raise ModelError("MODEL_CONFIG_INVALID") from None

    def _settle(self, request, reply, amount, started):
        usage = reply.usage
        known = (isinstance(usage, Usage)
                 and all(type(number) is int for number in (usage.input_tokens, usage.output_tokens, usage.cached_input_tokens))
                 and 0 <= usage.cached_input_tokens <= usage.input_tokens <= self.rate.max_input_tokens
                 and 0 <= usage.output_tokens <= request.output_tokens)
        if reply.billed_nano_usd is not None or self.config.provider == "kakou":
            known = known and type(reply.billed_nano_usd) is int and 0 <= reply.billed_nano_usd <= amount
        cost = amount
        if known:
            cost = self.rate.cost(usage) if reply.billed_nano_usd is None else reply.billed_nano_usd
        error = reply.error if reply.error in PROVIDER_ERRORS else "MODEL_UNAVAILABLE" if reply.error else None
        candidate, digest = None, None
        if not known:
            error = error or "MODEL_USAGE_UNCERTAIN"
        if error is None:
            try:
                encoded = reply.content.encode("utf-8")
                if len(encoded) > MAX_OUTPUT_BYTES:
                    error = "MODEL_OUTPUT_TOO_LARGE"
                else:
                    candidate = strict_object(encoded)
                    digest = sha256(encoded).hexdigest()
            except (AttributeError, ProtocolError, RecursionError, ValueError):
                candidate, error = None, "MODEL_INVALID_OUTPUT"
        return Result(Response(
            request.request_id, self.config.model, request.prompt_version, amount, cost, usage if known else None,
            known and reply.exact_bill, round((monotonic() - started) * 1000), digest, error, usage_uncertain=not known,
        ), candidate)


class MockProvider:
    def invoke(self, request: Request, stop: Event) -> ProviderReply:
        if request.prompt_version == DEVELOPMENT_PROMPT_VERSION:
            try:
                source = strict_object(request.input_text)["files"]["app.js"]
                spec = strict_object(request.input_text)["spec"]
                criteria = spec["acceptanceCriteria"]
                response = {
                    "version": DEVELOPMENT_PROMPT_VERSION,
                    "patches": [{
                        "plan": "Return the current template source as a deterministic fixture patch.",
                        "specRef": {"type": "acceptanceCriteria", "id": criteria[0]["id"]},
                        "path": "app.js", "expectedSha256": source["sha256"],
                        "replacement": source["content"] + "\n/* deterministic mock repair */\n",
                        "expectedChecks": ["resource_integrity"],
                    }],
                }
                return ProviderReply(dumps(response, ensure_ascii=False), Usage(24, 12))
            except (KeyError, IndexError, TypeError, ValueError):
                return ProviderReply(None, Usage(0, 0), "MODEL_INVALID_OUTPUT")
        if request.prompt_version == DRAFT_PROMPT_VERSION:
            input_value = strict_object(request.input_text)
            draft = mock_draft(input_value["userMessage"])
            user_input = input_value.get("userInput")
            if user_input is not None:
                previous = input_value["previousDraft"]
                answer = user_input["text"]
                if user_input["type"] == "phone.revise":
                    draft = mock_draft(MOCK_NOTES_REQUEST)
                    draft["goal"] = previous["goal"] or answer
                elif not previous.get("goal", "").strip():
                    draft = {**previous, "goal": answer}
                elif previous.get("storesData") is None:
                    draft = mock_draft(MOCK_NOTES_REQUEST)
                    draft["goal"] = previous["goal"] or answer
            return ProviderReply(dumps(draft, ensure_ascii=False), Usage(24, 12))
        return ProviderReply('{"goal":"Keep short notes","storesData":true}', Usage(24, 12))
