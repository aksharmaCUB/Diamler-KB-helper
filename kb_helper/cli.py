"""Terminal chat: python -m kb_helper.cli"""
from __future__ import annotations

import argparse
import logging
import sys

from .agent import Assistant
from .config import load_settings
from .connectors import ConnectorError, build_connectors


def _print_event(event: dict) -> None:
    if event["type"] == "search":
        where = f" in {event['connector']}" if event.get("connector") else ""
        print(f"  [searching{where}: {event['query']}]", flush=True)
    elif event["type"] == "read":
        print(f"  [reading {event['connector']}:{event['document_id']}]", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chat with the knowledge-base helper in the terminal")
    parser.add_argument("--config", help="Path to config.yaml")
    parser.add_argument("-q", "--question", help="Ask one question and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    settings = load_settings(args.config)
    try:
        connectors = build_connectors(settings.connectors)
    except ConnectorError as exc:
        print(f"Connector configuration error: {exc}", file=sys.stderr)
        return 2
    assistant = Assistant(
        connectors,
        model=settings.model,
        effort=settings.effort,
        max_tokens=settings.max_tokens,
        fallbacks=settings.fallbacks,
        extra_instructions=settings.extra_instructions,
        max_tool_rounds=settings.max_tool_rounds,
    )
    history: list[dict] = []

    def ask(text: str) -> list[str]:
        turn = assistant.respond(history, text, on_event=_print_event)
        print()
        print(turn.text)
        if turn.options:
            print("\nOptions: " + " | ".join(f"[{i + 1}] {o}" for i, o in enumerate(turn.options)))
        if turn.sources:
            print("\nSources:")
            for source in turn.sources:
                print(f"  - {source.title}" + (f" <{source.url}>" if source.url else ""))
        print()
        return turn.options

    if args.question:
        ask(args.question)
        return 0

    print(f"KB helper ({assistant.model}) - sources: {', '.join(connectors) or 'none'}")
    print("Type your question. Commands: /reset, /connectors, /quit\n")
    last_options: list[str] = []
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not text:
            continue
        if text in {"/quit", "/exit"}:
            return 0
        if text == "/reset":
            history.clear()
            print("(conversation cleared)\n")
            continue
        if text == "/connectors":
            for connector in connectors.values():
                print(f"  - {connector.name} ({connector.type_name}): {connector.description}")
            print()
            continue
        if text.isdigit() and last_options and 1 <= int(text) <= len(last_options):
            text = last_options[int(text) - 1]
            print(f"you> {text}")
        try:
            last_options = ask(text)
        except KeyboardInterrupt:
            print("\n(interrupted)\n")
            last_options = []


if __name__ == "__main__":
    sys.exit(main())
