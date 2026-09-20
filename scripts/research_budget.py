#!/usr/bin/env python3
"""Stop scheduled model work once recorded monthly usage reaches its allowance."""

import json
import os
import urllib.request


def main() -> None:
    base = os.environ["KAUPO_API_URL"].rstrip("/")
    if not base.startswith("https://"):
        raise ValueError("the API requires HTTPS")
    request = urllib.request.Request(  # noqa: S310 -- HTTPS checked above
        base + "/api/v1/research/budget",
        headers={"Authorization": "Bearer " + os.environ["KAUPO_RESEARCH_TOKEN"]},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        budget = json.load(response)
    if budget["recorded_spend_eur"] >= budget["budget_eur"]:
        raise SystemExit("Recorded research usage reached the monthly budget. Paid review stopped.")
    if not budget["coverage_complete"]:
        print("::warning::Uncovered usage is unknown. This is not a provider spending cap.")
    print(json.dumps(budget))


if __name__ == "__main__":
    main()
