"""macOS menu bar app — the user-facing surface for teammates.

Click the menubar icon → Enrich CSV… → native file picker → progress shown
in the dropdown title → notification + reveal-in-Finder when done.

Settings (GitHub token, skip-SMTP toggle) are persisted via rumps.App config.
"""
from __future__ import annotations

import csv
import os
import subprocess
import threading
from typing import Optional

try:
    import rumps  # type: ignore
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        "rumps not installed. Run `pip install rumps` or use the packaged .app."
    ) from e

from founder_enrich.cli import write_output
from founder_enrich.pipeline import enrich_rows


APP_NAME = "FounderEnrich"
ICON_TITLE = "✉︎"  # menu bar icon — kept as a glyph so we don't ship an .icns asset


class FounderEnrichApp(rumps.App):
    def __init__(self) -> None:
        super().__init__(APP_NAME, title=ICON_TITLE, quit_button=None)
        self.menu = [
            rumps.MenuItem("Enrich CSV…", callback=self.on_enrich),
            None,
            rumps.MenuItem("Skip SMTP verification", callback=self.on_toggle_smtp),
            rumps.MenuItem("Set Exa API key…", callback=self.on_set_exa_key),
            rumps.MenuItem("Clear cache", callback=self.on_clear_cache),
            None,
            rumps.MenuItem("Quit", callback=rumps.quit_application),
        ]
        # Hydrate persisted settings.
        self._skip_smtp = bool(self._read_config("skip_smtp", False))
        self.menu["Skip SMTP verification"].state = self._skip_smtp
        self._busy = False

    # ---------- menu callbacks ----------

    def on_enrich(self, _: rumps.MenuItem) -> None:
        if self._busy:
            rumps.notification(APP_NAME, "Busy", "Already enriching a file.")
            return
        path = _pick_csv()
        if not path:
            return
        threading.Thread(target=self._run_job, args=(path,), daemon=True).start()

    def on_toggle_smtp(self, item: rumps.MenuItem) -> None:
        self._skip_smtp = not self._skip_smtp
        item.state = self._skip_smtp
        self._write_config("skip_smtp", self._skip_smtp)

    def on_set_exa_key(self, _: rumps.MenuItem) -> None:
        current = self._read_config("exa_api_key", "") or ""
        resp = rumps.Window(
            message=(
                "The single biggest accuracy lever. Exa People Search "
                "(1B-profile index) verifies founders against work history "
                "instead of guessing from page text.\n\n"
                "Free tier: 1000 searches/month. Sign up at "
                "https://dashboard.exa.ai"
            ),
            title="Exa API Key",
            default_text=current,
            ok="Save",
            cancel="Cancel",
            dimensions=(360, 100),
        ).run()
        if resp.clicked:
            self._write_config("exa_api_key", resp.text.strip())
            rumps.notification(APP_NAME, "Saved", "Exa API key updated.")

    def on_clear_cache(self, _: rumps.MenuItem) -> None:
        from founder_enrich import cache as _cache
        removed = _cache.clear()
        rumps.notification(APP_NAME, "Cache cleared", f"{removed} entries removed.")

    # ---------- job runner ----------

    def _run_job(self, path: str) -> None:
        self._busy = True
        original_title = self.title
        try:
            with open(path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                if not reader.fieldnames:
                    raise ValueError("CSV has no header row")
                rows = list(reader)
                in_fieldnames = list(reader.fieldnames)

            company_col, domain_col = _detect_columns(in_fieldnames)
            if not domain_col:
                rumps.alert(
                    "Missing domain column",
                    "Your CSV needs a column named 'domain' (or 'website', "
                    "'url'). Found: " + ", ".join(in_fieldnames),
                )
                return

            exa_key = self._read_config("exa_api_key", "") or None

            def progress(done: int, total: int, _domain: str) -> None:
                self.title = f"{ICON_TITLE} {done}/{total}"

            results = enrich_rows(
                rows,
                company_col=company_col,
                domain_col=domain_col,
                exa_api_key=exa_key,
                do_smtp=not self._skip_smtp,
                max_workers=5,
                on_progress=progress,
            )

            out_path = _default_output(path)
            write_output(rows, results, company_col, domain_col, out_path)

            high = sum(1 for r in results if r.confidence == "high")
            med = sum(1 for r in results if r.confidence == "medium")
            rumps.notification(
                APP_NAME,
                "Done",
                f"{high} verified + {med} likely of {len(results)}. Click to reveal.",
                sound=True,
            )
            subprocess.run(["open", "-R", out_path], check=False)
        except Exception as e:
            rumps.alert("Enrichment failed", f"{type(e).__name__}: {e}")
        finally:
            self.title = original_title
            self._busy = False

    # ---------- config persistence ----------

    def _config_path(self) -> str:
        d = os.path.expanduser("~/Library/Application Support/FounderEnrich")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "config.json")

    def _read_config(self, key: str, default):
        import json
        try:
            with open(self._config_path(), encoding="utf-8") as f:
                return json.load(f).get(key, default)
        except (FileNotFoundError, ValueError):
            return default

    def _write_config(self, key: str, value) -> None:
        import json
        path = self._config_path()
        data = {}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, ValueError):
            pass
        data[key] = value
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)


# ---------- helpers ----------


def _pick_csv() -> Optional[str]:
    """Native file picker via AppleScript — no extra dependency."""
    script = (
        'set f to choose file with prompt "Choose companies CSV" '
        'of type {"csv", "public.comma-separated-values-text", "txt"}\n'
        "POSIX path of f"
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip() or None
    except subprocess.CalledProcessError:
        return None  # user cancelled


def _detect_columns(fieldnames):
    """Find company + domain columns by exact-then-substring match. Real-world
    CSVs from CRMs have column names like 'Parent Record > Domains' that exact
    aliases miss but substring matching catches reliably."""
    company_keywords = ("company", "organization", "record", "name", "account")
    domain_keywords = ("domain", "website", "url", "site")
    lower = {f.lower(): f for f in fieldnames}

    def pick(keywords):
        # Prefer exact match, then substring match. Skip columns named "name"
        # if a more specific match exists, to avoid grabbing person columns
        # in CRM exports.
        for kw in keywords:
            if kw in lower:
                return lower[kw]
        for kw in keywords:
            for orig in fieldnames:
                if kw in orig.lower():
                    return orig
        return None

    company = pick(company_keywords) or fieldnames[0]
    domain = pick(domain_keywords)
    return company, domain


def _default_output(in_path: str) -> str:
    base, ext = os.path.splitext(in_path)
    return f"{base}_enriched{ext or '.csv'}"


def main() -> None:
    FounderEnrichApp().run()


if __name__ == "__main__":
    main()
