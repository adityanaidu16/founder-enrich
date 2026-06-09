# FounderEnrich

Mac menu bar tool that takes a CSV of companies + domains and writes back a
clean CSV of founders and their emails. Built for venture outbound as a free,
open-source replacement for the manual Specter workflow.

**Architecture in one line:** local site scraper for free coverage, [Exa
People Search](https://dashboard.exa.ai) (1B-profile index, 1000 free
searches/month) for verified founders, optional SMTP check for
deliverability. No hosted services, no shared state — everything runs on the
teammate's Mac.

## For teammates

1. Download `FounderEnrich-macos.zip` from
   [Releases](../../releases) → unzip → drag **FounderEnrich.app**
   to your **Applications** folder.
2. **One-time Gatekeeper bypass.** The app isn't signed with an Apple
   Developer cert ($99/yr), so modern macOS blocks it on first launch.
   Open **Terminal** and run:
   ```bash
   xattr -dr com.apple.quarantine /Applications/FounderEnrich.app
   open /Applications/FounderEnrich.app
   ```
   This strips the quarantine flag macOS attached when you downloaded the
   zip. Run it once; the app opens cleanly forever after.
3. The ✉︎ icon appears in your menu bar. Click → **Enrich CSV…** → pick your
   file. The enriched copy is saved next to the original as
   `<filename>_enriched.csv` and Finder pops open to reveal it.

### One-time setup

Click ✉︎ → **Set Exa API key…** → paste a key from
<https://dashboard.exa.ai> (free signup, no credit card, 1000 searches/month
which covers ~1000 companies). This is the only key the tool uses and the
single biggest accuracy lever — without it, you'll find founders for ~30%
of typical stealth-stage prospects; with it, ~95%+.

### Input format

Any CSV with at least a company name column and a domain column. Column names
are auto-detected by substring match — these all work without flags:

| company column matches | domain column matches |
|---|---|
| `company`, `organization`, `record`, `name`, `account` | `domain`, `website`, `url`, `site` |

So a Specter / CRM export with `"Parent Record > Domains"` is detected
automatically.

### Output format

The enriched CSV is intentionally minimal — your input company and domain
columns plus three founder slots, nothing else:

```
<company column>, <domain column>,
founder_1_first_name, founder_1_last_name, founder_1_email,
founder_2_first_name, founder_2_last_name, founder_2_email,
founder_3_first_name, founder_3_last_name, founder_3_email
```

Empty slots stay empty (a company with one founder gets eight blank columns,
not padding).

## How it works

For each row:

1. **Cache check** — local SQLite (per-teammate, never synced), 30-day TTL.
   Same domain looked up before? Instant return, no network calls.
2. **Site parser** — scrape `/team`, `/about`, `/founders`, `/leadership`
   pages on the company's own site. Free, no key. Catches well-documented
   startups; misses stealth-stage and JS-only sites.
3. **Exa People Search** — *runs only if the parser came up empty or weak*.
   Queries Exa's 1B-profile index for `"founder co-founder {company}"`,
   verifies each result client-side by checking work history mentions the
   target company. Skipped silently if no key is configured.
4. **Pattern resolution** — DNS MX lookup, infer email pattern from any
   anchor email found in step 2, generate candidate emails per founder.
5. **SMTP verification** — `RCPT TO` check with catch-all detection.
   Gracefully degrades when port 25 is blocked (common on home/coffee-shop
   wifi) or when the target uses Google Workspace (returns 250 to
   everything).

## Privacy and data flow

Anything sensitive about your prospect list stays on your Mac:

- Local cache lives in `~/Library/Application Support/FounderEnrich/cache.db`
  and is never synced.
- No queries are logged centrally; no shared API keys; no central operator.
- Outbound traffic only goes to: the target company's own website (parser),
  `api.exa.ai` (if you've added a key), and the target's MX server (SMTP
  verify).

## For developers

```bash
pip install -r founder_enrich/requirements.txt

# Run the menu bar app from a terminal (icon appears in menu bar)
python -m founder_enrich.app

# Headless CLI against a CSV
EXA_API_KEY=... python -m founder_enrich path/to/input.csv

# Custom column names (substring match also works without flags)
python -m founder_enrich input.csv --company-col Record --domain-col Domains

# Build the .app bundle locally
python founder_enrich/setup.py py2app
open dist/FounderEnrich.app
```

### Cutting a release

```bash
git tag enricher-v0.2.0
git push origin enricher-v0.2.0
```

GitHub Actions builds the `.app`, zips it, and attaches it to the
[Releases page](../../releases). Teammates download from there.

## Honest caveats

- **With Exa key, hit rate is ~95%** on typical VC-style outbound CSVs (mix
  of stealth and growth-stage). Without it, parser-only is ~30-50% — fine
  for later-stage targets, misses most stealth.
- **SMTP verification is unreliable for Google Workspace domains** — they
  accept any address. The tool detects this (`catch-all`) and downgrades
  confidence rather than lying.
- **Residential ISPs frequently block outbound port 25**, which breaks SMTP
  verification entirely. The tool returns `smtp-blocked` and relies on
  pattern confidence instead. Office wifi usually works.
- **`all_founders` may contain the same person twice in different forms** —
  e.g. "Jaber J." in primary, "Osama Jaber" in secondary. Worth a manual
  glance before pasting into your sequencer.
- **Pattern-guessed emails aren't guaranteed deliverable.** Treat
  unverified outputs as research leads, run them through your sequencer's
  deliverability filter, or wire up Hunter.io / NeverBounce for batch
  verification before sending.

## License

[MIT](LICENSE). Views and analysis are your own.
