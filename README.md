# NSE Weekly/Monthly Scanner — Daily Email via GitHub Actions

Scans a universe of NSE stocks using VStop + KAMA + RSI + Volume + OBV +
ADX + NIFTY regime + relative-strength filters, and emails you the top 20
BUY and top 20 SELL/trend-break signals every trading day — for free,
without your laptop needing to be on.

## 1. Create the repo

1. Create a **new GitHub repository** (private is fine — see the note on
   Actions minutes below).
2. Upload all the files in this folder, preserving the `.github/workflows/`
   path (that's what makes it a scheduled workflow).

## 2. Create a Gmail App Password

1. myaccount.google.com → Security → turn on 2-Step Verification (if not
   already on).
2. Search "App passwords" in the same Security settings → create one named
   "NSE Scanner" → copy the 16-character password shown.

## 3. Add GitHub Secrets

In your repo: **Settings → Secrets and variables → Actions → New repository
secret**. Add these three:

| Secret name | Value |
|---|---|
| `SCANNER_EMAIL_FROM` | the Gmail address sending the mail |
| `SCANNER_EMAIL_TO` | where you want the report — e.g. `umesh.karjinni@gmail.com` |
| `SCANNER_EMAIL_APP_PASSWORD` | the 16-character app password from step 2 |

Never put these directly in the code — Secrets are encrypted and never
appear in logs.

## 4. Trigger a run

Scheduled workflows only start running once GitHub has seen the workflow
file on the default branch, and won't fire on the very first push. Go to
**Actions → NSE Daily Scanner → Run workflow** to trigger it manually the
first time and confirm everything works end-to-end.

After that it runs automatically at **11:00 AM IST, Monday–Friday**
(NSE trading days). To change the time, edit the `cron:` line in
`.github/workflows/daily-scan.yml` — GitHub Actions cron is always in UTC,
so subtract 5 hours 30 minutes from your desired IST time.

## 5. What you'll get

Every run emails an HTML report with:
- A table of the **top 20 BUY signals** (ranked by ADX / trend strength)
- A table of the **top 20 SELL / trend-break signals**
- Small inline charts for up to 10 of the top BUY signals
- The full, untrimmed results (every signal, not just the top 20) are
  attached to the **workflow run itself** as a downloadable artifact —
  go to Actions → the specific run → Artifacts, to grab the complete CSV.

Adjust `TOP_N_BUY` / `TOP_N_SELL` near the top of `nse_scanner.py` if you
want a different cutoff than 20.

## 6. Growing beyond the starter stock list

`stocks_universe.csv` ships with ~140 well-known large/mid/small cap
stocks. To scan closer to the full NSE universe (~1500+ stocks):

1. Run `python build_universe.py` **on your own machine** (NSE actively
   blocks a lot of automated/datacenter traffic, so this is more reliable
   from a residential connection than from GitHub's own servers).
2. It produces `stocks_universe_full.csv`.
3. Commit that file to the repo, then change this line near the top of
   `nse_scanner.py`:
   ```python
   UNIVERSE_CSV = "stocks_universe_full.csv"
   ```
4. Push the change — the next scheduled run picks it up automatically.

Scanning ~1500 stocks takes considerably longer (expect 45–90+ minutes)
— the workflow's `timeout-minutes: 150` already accounts for this, but
double-check your run doesn't get cut off in the Actions log.

## 7. A few things worth knowing

- **Public vs private repo**: GitHub Actions is unlimited/free on public
  repos. Private repos get 2,000 free minutes/month — a large universe
  scan run daily on weekdays could approach that limit. If you want the
  code private, keep an eye on **Settings → Billing → Actions usage**.
- **Inactive repos**: GitHub automatically disables scheduled workflows
  after 60 days with no repository activity. A commit, even a trivial one,
  re-enables it.
- **This isn't financial advice.** The scanner is a technical
  decision-support tool with no fundamental or macro context. Validate
  signals yourself before acting on them.
