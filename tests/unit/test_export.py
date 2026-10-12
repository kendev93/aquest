from __future__ import annotations

import csv
import io
import json

from aquest.screening.export import report_to_csv, report_to_json
from aquest.screening.models import ScanReport, ScreenMatch


def sample_report() -> ScanReport:
    return ScanReport(
        total_candidates=1,
        total_scanned=1,
        total_signals=1,
        errors=0,
        skipped=0,
        results=(
            ScreenMatch(
                market="SH",
                code="600000",
                signal_date=20260824,
                last_close=12.35,
                matched_signals=("indicator_one.main_force_entry",),
                match_count=1,
                indicator_values={"indicator_one.var5": 1.23},
            ),
        ),
        failure_reasons={},
        skip_reasons={},
    )


def test_json_export_contains_result_and_summary() -> None:
    payload = json.loads(report_to_json(sample_report()))

    assert payload["summary"]["total_signals"] == 1
    assert payload["results"][0]["code"] == "600000"


def test_csv_export_contains_headers_and_signal_rows() -> None:
    rows = list(csv.DictReader(io.StringIO(report_to_csv(sample_report()))))

    assert rows[0]["market"] == "SH"
    assert rows[0]["matched_signals"] == "indicator_one.main_force_entry"
    assert rows[0]["indicator_values"] == '{"indicator_one.var5": 1.23}'
