"""CLI front-end. Mostly for headless testing; teammates use the menu bar app."""
from __future__ import annotations

import argparse
import csv
import os
import sys
from typing import List

from founder_enrich.pipeline import MAX_FOUNDERS, RowResult, enrich_rows, split_full_name

# Output is intentionally minimal: company column + domain column + up to
# MAX_FOUNDERS slots, each split into first_name / last_name / email. No
# pass-through of other input columns, no metadata.
def _founder_cols(n: int):
    return [
        f"founder_{n}_first_name",
        f"founder_{n}_last_name",
        f"founder_{n}_email",
    ]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="founder_enrich")
    parser.add_argument("input_csv")
    parser.add_argument("-o", "--output", help="output CSV path (default: <input>_enriched.csv)")
    parser.add_argument("--company-col", default="company")
    parser.add_argument("--domain-col", default="domain")
    parser.add_argument("--smtp", action="store_true",
                        help="run SMTP RCPT TO verification (slower; "
                             "rarely useful for Google Workspace domains)")
    parser.add_argument("--workers", type=int, default=5)
    args = parser.parse_args(argv)

    out_path = args.output or _default_output(args.input_csv)
    exa_key = os.environ.get("EXA_API_KEY") or None

    with open(args.input_csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            print("error: input CSV has no header row", file=sys.stderr)
            return 2
        if args.domain_col not in reader.fieldnames:
            # Substring-fallback: catches CRM-style 'Parent Record > Domains'.
            substr_match = next(
                (f for f in reader.fieldnames if args.domain_col.lower() in f.lower()),
                None,
            )
            if substr_match:
                print(f"using domain column '{substr_match}'", file=sys.stderr)
                args.domain_col = substr_match
            else:
                print(
                    f"error: column '{args.domain_col}' not found. "
                    f"Available: {reader.fieldnames}",
                    file=sys.stderr,
                )
                return 2
        rows = list(reader)
        in_fieldnames = list(reader.fieldnames)

    print(f"enriching {len(rows)} rows → {out_path}", file=sys.stderr)

    def progress(done: int, total: int, domain: str) -> None:
        print(f"  [{done}/{total}] {domain}", file=sys.stderr)

    results = enrich_rows(
        rows,
        company_col=args.company_col,
        domain_col=args.domain_col,
        exa_api_key=exa_key,
        do_smtp=args.smtp,
        max_workers=args.workers,
        on_progress=progress,
    )

    write_output(rows, results, args.company_col, args.domain_col, out_path)
    print(_summary(results), file=sys.stderr)
    return 0


def write_output(
    input_rows: List[dict],
    results: List[RowResult],
    company_col: str,
    domain_col: str,
    out_path: str,
) -> None:
    """Write minimal enriched CSV: company + domain + up to MAX_FOUNDERS
    founder slots split into first_name/last_name/email. All other input
    columns are dropped intentionally."""
    out_cols = [company_col, domain_col]
    for i in range(1, MAX_FOUNDERS + 1):
        out_cols.extend(_founder_cols(i))

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_cols)
        writer.writeheader()
        for original, result in zip(input_rows, results):
            row = {
                company_col: original.get(company_col, ""),
                domain_col: original.get(domain_col, ""),
            }
            # Slot 1 = primary; slots 2+ pulled from all_founders/all_emails.
            names = [result.founder_name] + list(result.all_founders)
            emails = [result.email] + list(result.all_emails)
            for i in range(MAX_FOUNDERS):
                first, last = split_full_name(names[i]) if i < len(names) else ("", "")
                email = emails[i] if i < len(emails) else ""
                row[f"founder_{i+1}_first_name"] = first
                row[f"founder_{i+1}_last_name"] = last
                row[f"founder_{i+1}_email"] = email
            writer.writerow(row)


def _summary(results: List[RowResult]) -> str:
    from collections import Counter
    counts = Counter(r.confidence for r in results)
    return (
        f"done: high={counts.get('high', 0)} "
        f"medium={counts.get('medium', 0)} "
        f"low={counts.get('low', 0)} "
        f"none={counts.get('none', 0)} "
        f"of {len(results)}"
    )


def _default_output(in_path: str) -> str:
    base, ext = os.path.splitext(in_path)
    return f"{base}_enriched{ext or '.csv'}"


if __name__ == "__main__":
    sys.exit(main())
