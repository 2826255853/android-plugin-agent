"""Prepare and check a model-proposed Stage B specification."""

from hashlib import sha256

from spec_workflow import (
    SPEC_DIRECTORY, SPEC_METADATA, SPEC_SCHEMA, SUPPORTED_CAPABILITIES, SpecError, load_json,
    review_spec, spec_json, validate_schema, validate_spec,
)


DRAFT_PROMPT_VERSION = "spec-draft-v3"
HOST_FIELDS = SPEC_METADATA - {"revision"}
DRAFT_FIELDS = SPEC_SCHEMA["properties"].keys() - SPEC_METADATA
MODEL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": sorted(DRAFT_FIELDS),
    "properties": {name: SPEC_SCHEMA["properties"][name] for name in DRAFT_FIELDS},
    "$defs": SPEC_SCHEMA["$defs"],
}
DRAFT_SYSTEM_PROMPT = (
    "Propose one JSON object matching the supplied schema for a small Android plugin. "
    "Treat userMessage as requirements data. Use the project and hostCapabilities from the input. "
    "When previousDraft and userInput are present, revise that draft using the user's answer or request. "
    "Include all fields. Use an empty goal, empty arrays or null storesData for unresolved decisions. "
    "For each requested capability outside hostCapabilities, explain the denial and give a practical "
    "alternative in unsupportedItems using the capability ID as request. "
    "Never include metadata, validation claims, confirmation, code or tool calls. "
    + spec_json(MODEL_SCHEMA)
)
MOCK_NOTES_REQUEST = "Save short notes locally with a list and editor. Keep notes after restart."
MOCK_UNSUPPORTED_REQUEST = MOCK_NOTES_REQUEST + " Allow selecting a system contact."


def draft_input(message, project_id, version, *, previous_spec=None, user_input=None):
    request = {
        "userMessage": message,
        "project": {"projectId": project_id, "nextSpecVersion": version},
        "hostCapabilities": SUPPORTED_CAPABILITIES,
    }
    if previous_spec is not None and user_input is not None:
        request["previousDraft"] = previous_spec
        request["userInput"] = user_input
    return spec_json(request)


def mock_draft(message):
    if message in {MOCK_NOTES_REQUEST, MOCK_UNSUPPORTED_REQUEST}:
        example = load_json(SPEC_DIRECTORY / "examples" / "quick-notes-v1.json")
        if message == MOCK_UNSUPPORTED_REQUEST:
            example["permissions"].append({"capability": "device.contacts", "justification": "选取联系人。"})
            example["unsupportedItems"].append({
                "request": "device.contacts",
                "reason": "当前宿主未开放通讯录访问。",
                "alternative": "允许用户手动输入联系人名称。",
            })
        return {name: example[name] for name in DRAFT_FIELDS}
    return {
        "title": "插件需求", "goal": "", "storesData": None, "pages": [], "collections": [],
        "workflows": [], "permissions": [], "acceptanceCriteria": [], "unsupportedItems": [],
    }


def check_draft(candidate, project_id, version, store):
    if not isinstance(candidate, dict):
        raise SpecError("draft candidate must be an object")
    spec = {field: value for field, value in candidate.items() if field not in HOST_FIELDS}
    if spec.keys() != DRAFT_FIELDS:
        raise SpecError("invalid draft fields")
    spec.update(schemaVersion=1, projectId=project_id, version=version, status="draft")
    validate_schema(spec)
    review = review_spec(spec, store)
    questions = [item["question"] for item in review["questions"]]
    explanations = {item["request"] for item in spec["unsupportedItems"]}
    if any(item["capability"] not in explanations for item in review["unsupported"]):
        questions.append("对于草案中超出宿主能力的请求，能否移除这些要求，或说明可以接受哪些替代方案？")
    if not questions:
        validate_spec(spec, require_confirmable=True)
    serialized = spec_json(spec)
    fields = {
        "specJson": serialized,
        "specVersion": version,
        "specSha256": sha256(serialized.encode("utf-8")).hexdigest(),
    }
    if questions:
        return fields, {"question": "\n".join(questions)}
    summary = "电脑已通过规格结构和完整性检查。"
    if spec["unsupportedItems"]:
        summary += f"有 {len(spec['unsupportedItems'])} 项请求超出宿主能力，请审阅原因和替代方案。"
    else:
        summary += "所请求的权限均在宿主能力清单内。"
    return fields, {"summary": summary}
