"""Deterministic JSON and CSV serialization for scan reports."""

from __future__ import annotations

import csv
import io
import json

from .models import ScanReport


def report_to_json(report: ScanReport) -> str:
    payload = {
        "summary": report.summary_dict(),
        "results": [result.to_dict() for result in report.results],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def report_to_csv(report: ScanReport) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=[
            "market",
            "code",
            "name",
            "instrument_type",
            "board",
            "signal_date",
            "last_close",
            "matched_signals",
            "match_count",
            "indicator_values",
        ],
    )
    writer.writeheader()
    for result in report.results:
        writer.writerow(
            {
                **result.to_dict(),
                "matched_signals": ",".join(result.matched_signals),
                "indicator_values": json.dumps(
                    dict(result.indicator_values), ensure_ascii=False, sort_keys=True
                ),
            }
        )
    return buffer.getvalue()
