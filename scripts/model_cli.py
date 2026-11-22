"""Configure the Windows credential or run one bounded model smoke request."""

import argparse
import json
import sys
import warnings
from dataclasses import asdict
from getpass import GetPassWarning, getpass
from uuid import uuid4

from model_boundary import Config, ModelBoundary, ModelError, MockProvider, Request
from model_credentials import save_key, save_openai_key
from openai_model import OpenAIProvider
from relay_model import RELAY_TARGET, RelayProvider


def smoke(provider, model, budget):
    config = Config(provider, model, budget)
    adapters = {"mock": MockProvider, "openai": OpenAIProvider, "kakou": RelayProvider}
    adapter = adapters[config.provider]()
    boundary = ModelBoundary(config, adapter)
    request = Request(uuid4(), "model-smoke", uuid4(), "A small tool to write and list short notes.",
                      timeout_seconds=30, output_tokens=512 if provider == "kakou" else 256)
    try:
        result = boundary.call(request)
    except ModelError as error:
        print(json.dumps({"request_id": str(request.request_id), "error": error.code}))
        return 1
    print(json.dumps(asdict(result.receipt), default=str))
    return int(result.receipt.error is not None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("set-openai-key", help="read a key without echo and store it in Windows Credential Manager")
    commands.add_parser("set-kakou-key", help="read a key without echo and store it in Windows Credential Manager")
    run = commands.add_parser("smoke", help="send one fixed synthetic request and print receipt metadata")
    run.add_argument("--provider", choices=["mock", "openai", "kakou"], required=True)
    run.add_argument("--model", required=True)
    run.add_argument("--budget-nano-usd", required=True, type=int)
    args = parser.parse_args()
    try:
        if args.command in ("set-openai-key", "set-kakou-key"):
            with warnings.catch_warnings():
                warnings.simplefilter("error", GetPassWarning)
                key = getpass("Model API key: ").strip()
                if args.command == "set-kakou-key":
                    save_key(RELAY_TARGET, key)
                else:
                    save_openai_key(key)
            print("Windows credential saved.")
            return 0
        return smoke(args.provider, args.model, args.budget_nano_usd)
    except (EOFError, GetPassWarning):
        print(json.dumps({"error": "MODEL_CONFIG_INVALID"}), file=sys.stderr)
        return 1
    except ModelError as error:
        print(json.dumps({"error": error.code}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
