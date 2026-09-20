#!/usr/bin/env python3
"""Preview or import actual usage charges. Prepaid deposits are not expenses."""

import argparse
import csv
import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any


def records(path: Path) -> list[dict[str, Any]]:
    result = []
    seen = set()
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            reference = "moonshot:" + row["reference"]
            if not row["reference"] or reference in seen:
                raise ValueError("every charge requires a unique provider reference")
            seen.add(reference)
            if row["kind"] != "usage":
                raise ValueError("only usage charges are expenses; omit prepaid deposits and credits")
            ts = datetime.fromisoformat(row["occurred_at"].replace("Z", "+00:00"))
            if ts.tzinfo is None:
                raise ValueError("occurred_at requires a timezone")
            amount, rate = Decimal(row["amount"]), Decimal(row["eur_per_unit"])
            if not amount.is_finite() or not rate.is_finite() or amount <= 0 or rate <= 0:
                raise ValueError("charge and EUR conversion rate must be finite and positive")
            if row["currency"] == "EUR" and rate != 1:
                raise ValueError("EUR charges require eur_per_unit=1")
            euros = (amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if euros <= 0:
                raise ValueError("aggregate sub-cent charges by provider day before import")
            result.append(
                {
                    "reference": reference,
                    "kind": "cost",
                    "amount_eur": str(euros),
                    "period_start": ts.isoformat(),
                    "period_end": ts.isoformat(),
                    "note": f"Actual Moonshot usage: {amount} {row['currency']}; EUR/unit {rate}. "
                    + row["source"],
                }
            )
    if not result:
        raise ValueError("no usage charges in the file")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    charges = records(args.csv)
    print(json.dumps(charges, indent=2))
    if not args.apply:
        return
    base = os.environ["KAUPO_API_URL"].rstrip("/")
    if not base.startswith("https://"):
        raise ValueError("the API requires HTTPS")
    for charge in charges:
        request = urllib.request.Request(  # noqa: S310 -- HTTPS checked above
            base + "/api/v1/research/costs",
            data=json.dumps(charge).encode(),
            headers={
                "Authorization": "Bearer " + os.environ["KAUPO_ADMIN_TOKEN"],
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            response.read()
    print(f"Imported {len(charges)} usage charges. Coverage requires separate operator attestation.")


if __name__ == "__main__":
    main()
