"""Seed a running service with the demo scenarios.

    python -m support_assistant.demo.seed [--base-url http://localhost:8000]

Goes in through the front door -- `POST /tickets`, then `GET` until the status is
terminal -- rather than writing rows into the database. A seeder that inserted tickets
directly would produce states the pipeline never actually reached, which is the one thing
a demo of this system must not do.

`urllib` rather than `httpx`: the service itself depends on neither, and a demo aid is a
poor reason to add a runtime dependency.

Every scenario declares the outcome it expects, so this doubles as a smoke test of the
whole stack over HTTP: it exits non-zero if any ticket lands somewhere else.
"""

import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from support_assistant.demo import load_scenarios

DEFAULT_BASE_URL = "http://localhost:8000"
POLL_INTERVAL_SECONDS = 0.1
POLL_ATTEMPTS = 100
"""Ten seconds. Ample under `FakeLLM`, which finishes in milliseconds, and enough headroom
for a local model server behind `LLM_PROVIDER=ollama`."""


def _request(url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"content-type": "application/json"} if data else {}
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 -- operator's own URL
        return json.loads(response.read())


def submit(base_url: str, scenario: dict[str, Any]) -> dict[str, Any]:
    """POST the scenario, then poll until the pipeline has finished with it."""
    accepted = _request(
        f"{base_url}/tickets",
        {key: scenario[key] for key in ("user_id", "subject", "body")},
    )
    for _ in range(POLL_ATTEMPTS):
        ticket = _request(f"{base_url}/tickets/{accepted['id']}")
        if ticket["status"] != "processing":
            return ticket
        time.sleep(POLL_INTERVAL_SECONDS)
    return ticket


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="where the service is")
    parser.add_argument(
        "--in-order",
        action="store_true",
        help="submit in file order; the default shuffles, so the list is not sorted by outcome",
    )
    args = parser.parse_args(argv)
    base_url = args.base_url.rstrip("/")

    scenarios = load_scenarios()
    if not args.in_order:
        random.shuffle(scenarios)

    print(f"Seeding {len(scenarios)} scenarios against {base_url}\n")

    mismatches = 0
    for scenario in scenarios:
        try:
            ticket = submit(base_url, scenario)
        except urllib.error.URLError as exc:
            print(f"could not reach {base_url}: {exc}", file=sys.stderr)
            print("is it running? `uvicorn support_assistant.api.app:app`", file=sys.stderr)
            return 2

        expected = scenario["expect"]
        matched = (
            ticket["status"] == expected["status"]
            and ticket["handoff_reason"] == expected["handoff_reason"]
        )
        mismatches += not matched
        print(
            f"  {'ok ' if matched else 'BAD'}  {scenario['label']:<38}"
            f"{ticket['status']:<12}{ticket['handoff_reason'] or '':<22}{ticket['id']}"
        )
        if not matched:
            print(
                f"       expected {expected['status']}"
                f" / {expected['handoff_reason'] or 'no handoff'}",
                file=sys.stderr,
            )

    print(f"\n{len(scenarios) - mismatches} of {len(scenarios)} reached the expected outcome.")
    print(
        f"Open {base_url}/ui to explore them -- the page holds only the ids it was given,"
        "\nso paste one of the ids above into its 'open by id' box."
    )
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
