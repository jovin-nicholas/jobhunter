# jobhunter

Local-first job matching. It searches job boards, drops jobs that don't fit your filters, scores the rest against your
resume with a local model (Laya first, Ollama as a fallback), saves everything to SQLite, and notifies you about the
best matches. The default setup needs no account, API key or paid service.

## Quick start

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-laya.txt   # Laya needs PyTorch
cp jobhunter.example.yaml jobhunter.yaml                              # edit: resume, searches, filters, model
cp .env.example .env                                                  # optional: notifications, CANDIDATE_NAMES
# put your resume in resumes/
ollama pull gemma4:e4b                                                # the fallback scorer; any local model works (below)
.venv/bin/python -m jobhunter check-config                            # also says which models are missing
.venv/bin/python -m jobhunter run --dry-run                           # scores everything, saves and sends nothing
.venv/bin/python -m jobhunter run
```

`jobhunter.example.yaml` holds only the choices you have to make; everything else has a default that suits most
people. Every option, with its default, is in [docs/settings.md](docs/settings.md), and
[examples/full.yaml](examples/full.yaml) is a complete example.

The first run takes longest: discovery finds about 1,500 ATS job links in the public job lists, and each posting is
downloaded once. Postings older than `search.max_age_hours` (status `stale`) and postings that no longer exist
(status `gone`) are then remembered and skipped. A posting whose details come back empty (Gem, Dover, ADP) may be a
hiccup, so it is asked for again on later runs and counts as `gone` once it has been empty for 24 hours. A dry run
remembers nothing, so every dry run downloads them again.

## Running on a schedule

By default jobhunter runs every hour. Change it in `jobhunter.yaml`:

```yaml
schedule:
  every: 30m                   # 15m to 24h
  quiet_hours: "22:00-07:00"   # optional: no runs (and so no notifications) in this window
  quiet_days: [sat, sun]       # optional
  timezone: America/New_York   # optional; the computer's local time by default
```

Then either keep `.venv/bin/python scheduler.py` running (restart it after changing `every`), or add one cron entry
that fires every 15 minutes and let jobhunter decide when a run is due:

```
*/15 * * * * /path/to/jobhunter/run_cron.sh
```

`run --scheduled` (what both use) skips with one log line during quiet time or before the next run is due. A plain
`run` always runs.

## Laya

Laya is the default scorer: a local decision model, about 1 second per job on an Apple-silicon GPU (the first job also
loads it, about 5 s). `laya.model` is a Hugging Face id such as `convaiinnovations/laya`, downloaded on the first run,
or the folder of a Laya you fine-tuned on your own job decisions. If Laya is not installed or its model is missing,
jobs fall through to `ollama`, and `check-config` says what is missing.

A fine-tuned checkpoint can decide alert, save or skip by itself, with cut-offs you can override in `jobhunter.yaml`
([docs/settings.md](docs/settings.md#scorers-required)); `check-config` prints the ones in use. To train your own, see
[training/README.md](training/README.md).

## Ollama: choosing a local model

`gemma4:e4b` is only a suggestion: any Ollama model that fits your computer can score jobs and write cover letters. Put
its name in `ollama: {model: ...}` and run `ollama pull <name>`; `check-config` says when a model is missing or
Ollama is not running, warns when the models in use need more memory than the computer can spare, and shows the free
disk space when a model still has to be pulled. A model needs:

- **About 4B parameters or more.** Smaller models often break the JSON answer or give every job the same score.
- **A context window of at least 4,096 tokens** (raise `num_ctx` for a long resume).
- **Free memory of about its download size plus 1-2 GB.** 8 GB of RAM fits `qwen3:4b`; 16 GB fits `gemma4:e4b`
  (9.6 GB) or `qwen3:8b`. Without a GPU expect a minute or more per job.

Judging jobs needs less than writing cover letters: in a test on 60 postings `qwen3:4b` picked good jobs as well as
`gemma4:e4b`, about 2.5 times faster, but wrote worse letters. So pick the scoring model to fit your computer and give
cover letters the largest model it can run:

```yaml
scorers:
  - ollama: {model: "qwen3:4b"}         # judges jobs
cover_letters:
  enabled: true
  writer: ollama
  options: {model: "gemma4:e4b"}        # writes the letters
```

## Optional filters

Under `filters:` in `jobhunter.yaml` (every option is in [docs/settings.md](docs/settings.md#filters)):

- **A System One checker** answers what the rules cannot: is this a software engineering role, is it only for current
  students, where is a job whose location names no place, and optionally does it require citizenship or a clearance.
  It needs Ollama 0.35 or later and `ollama pull nimble:9b-q4_K_M` (5.6 GB); only the job is sent, never your resume.

  ```yaml
  systemone: {model: "nimble:9b-q4_K_M", skip_if: [citizenship, clearance]}   # skip_if is optional
  ```

- **Contract and hourly jobs** are recognised from the title and description, since no board reports employment type:
  "contract" or "freelance" in the title, wording such as "C2C", "W2 only", "6-month contract" or "you will be paid on
  a 1099 basis", and a dollar rate such as "$55/hr". A product that handles contracts or 1099s ("smart contracts",
  "Form 1099 processing") does not count. A skipped job is saved as `filtered` with the matched words as its reason.

  ```yaml
  employment:
    exclude: [contract, hourly]   # either or both
    contract_to_hire: keep        # or exclude
    hourly_internships: keep      # or exclude
  ```

## Boards

All 13 built-in boards run unless `boards:` lists others: `dice`, `linkedin`, `industry_jobs` (dev.to),
`top_companies` (Amazon, SoFi, Stripe), `hydepark`, and the ATS boards `greenhouse`, `lever`, `ashby`, `workday`,
`dover`, `adp` and `gem`, which read postings found by discovery in the public job lists, and `vc_boards` (below). Greenhouse, Lever, Ashby,
Gem and ADP can also list whole company boards: `greenhouse: {companies: [stripe, airbnb]}`; Dover also reads its
public feed of every company's jobs (`job_board: true`). With `discovery.getro` or `discovery.consider` set, VC portfolio job boards (Redpoint, Accel,
a16z, ...) add their startups' jobs: a Greenhouse, Lever, Ashby, Workday, Gem, Dover or ADP link goes to that board when
it runs with `discover` on, and every other link, including an ATS link whose board is off, not discovering or left out
by `--only`, comes from `vc_boards`. An explicit `boards:` list must include `vc_boards` to get these jobs.

- **linkedin** searches through the `linkedin-jobs-mcp` server, which needs Node.js (`npx`; the first run downloads
  it). The version is pinned (`MCP_PACKAGE` in `jobhunter/boards/linkedin.py`) and the server runs in `data/npx`, a
  folder only you can read (mode 700), not the repo folder. Without Node.js the board is skipped with a note. Descriptions come from LinkedIn's public job-posting pages,
  one request every 3 seconds.
- Requests are spaced per site; rate limits and server errors are retried with growing waits (or the site's
  `Retry-After`, up to 60 s), and a site that answers 429 is slowed down for every board.
- Rate-limited cloud API keys (`gemini`, `groq`) rest while the others are used; when all rest, jobs move on to the
  next scorer at once.

## Adding a job board

Drop a Python file in `plugins/` (see `plugins/example_feed.py`) and list its name under `boards:`:

```python
from jobhunter import Job, board

@board("my_board")
class MyBoard:
    def __init__(self, options):
        self.url = options["url"]

    def search(self, ctx):
        for item in ctx.http.get_json(self.url):
            yield Job(id=f"my_board_{item['id']}", title=item["title"], company=item["company"],
                      location=item["location"], url=item["url"], description=item["text"])
```

An optional `enrich(job, ctx)` method can fetch a full description for new jobs only. `ctx` also has `http` (one
polite session with retries), `discovery.urls()` (this run's ATS links), `is_known(job_id)` (already finished, so skip
fetching it), `mark_stale(job_id)` (too old) and `mark_gone(job_id)` (no longer exists); both are skipped on later
runs. `mark_empty(job_id)` records a details answer with no posting in it: the posting is asked for again, and becomes
`gone` once it has answered empty for 24 hours. A board class may set `default_timeout_s`, and may raise `BoardSkipped` when it cannot run on this machine.

Scorers work the same way with `@scorer("name")` and a `score(job, resume)` method returning a `ScoreResult`. A scorer
that also has `generate(prompt) -> str` can write cover letters.

## Commands

| Command | What it does |
|---|---|
| `run [--only dice,greenhouse] [--dry-run] [--scheduled]` | Search, filter, score, save and notify; `--dry-run` saves and sends nothing; `--scheduled` honours `schedule:` |
| `check-config` | Validate settings, plugins and resumes, then exit |
| `list-boards`, `list-scorers` | Built-in and plugin boards and scorers, and where each comes from |
| `export-feedback [--out FILE] [--since YYYY-MM-DD]` | Write each job's latest verdict from the alert buttons to a CSV (default `data/feedback_labels.csv`; columns `job_id,job_title,company,decision,verdict,received_at`) |
| `send-test-alert` | Email yourself an alert for the last notified job, to try the feedback buttons |

Each job is saved in `data/jobs.db` with a status: `notified`, `logged` or `skipped` (the score against
`decisions`), `filtered` (with the filter's reason), `stale` or `gone` (a discovered posting too old, or no longer
there), `duplicate` (would have been notified, but the same job was already notified in the last 30 days or earlier in
the run), `error_429_retry` (scored again when a board lists it again), `error_unavailable` / `error_notify` /
`error_scorer` (tried again on later runs: from the retry list for up to 24 hours after first being saved, and whenever a
board lists the job again; `error_notify` means no Slack or email channel delivered the alert, `error_scorer` that every
scorer failed on the job; an `error_scorer` job more than 24 hours old that no board lists again keeps that status
and is in effect final), `error_terminal` (a board listed an `error_scorer` job again more than 24 hours after it was
first saved, and every scorer still failed on it, or a board listed an `error` job again that long after and it crashed
again; not retried), or `error` (processing the job crashed; tried again when a board lists it again, for up to 24 hours
after it was first saved).
A board that needs its own description (e.g. `vc_boards`) saves a job as `filtered` instead of retrying it forever,
once the posting page gave no text for more than 6 hours or its URL is on a host that never allows automated
reading (e.g. Indeed).

A job is notified once: the same title at the same company counts as one job whatever board, id or location it comes
with. The company is matched as each board names it (see Known limitations).

## Feedback

Each alert email has four buttons: ✅ Applied, 👍 Good, 🤷 Maybe and 👎 Bad match. Tapping one opens a pre-filled email to
your own plus address (`you+jobhunter-feedback@gmail.com`); send it, and the next run reads it over IMAP with the same
Gmail app password, saves the verdict, and files the message under the label `jobhunter/feedback`. Nothing goes through
a server and nothing tracks opens. A message is trusted only from your own addresses and, when Gmail's
`Authentication-Results` header is there, only if it passed DKIM, SPF or DMARC. `export-feedback` writes each job's latest
verdict to `data/feedback_labels.csv`: a record of your verdicts, not yet a training input (turning them into training
pools is a manual step; docs/settings.md, notify).

To try it: `send-test-alert`, tap a button on your phone and send, `run --only <one board>` (the log shows
`feedback: 1 saved`), then `export-feedback` shows the row.

## Known limitations

- The same posting found on two boards (for example Stripe through `top_companies` and through `greenhouse`) is two
  jobs (one alert, since alerts match on title and company), and Workday job ids do not include the company.
- Lever and Ashby name a company by its board's address (`scaleai`), other boards by its display name ("Scale AI"),
  so the same role found on Lever or Ashby and on another board can be notified twice.
- A site's DNS answer is checked before a page download but could change in between (DNS rebinding); for a
  single-user tool this is accepted.
- A feedback email that Gmail added no `Authentication-Results` header to (one you sent to yourself may have none) is
  trusted by its From address alone, which a sender could forge.
- A board that times out is left behind, not stopped: its thread keeps running in the background until the run's
  process exits.
- Jobs are scored one at a time.

## Using job sites responsibly

jobhunter reads public job listings and posting pages, spaced per site and with backoff when a site asks it to slow
down. You are responsible for following each site's terms of use; turn off any board you should not use with
`boards:` in `jobhunter.yaml`.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The base install (`requirements.txt`) runs everything except the Laya training tests, which are skipped. The full
suite needs `requirements-laya.txt` and `requirements-train.txt` too. Tests that load a real checkpoint run only with
`LAYA_SLOW_TESTS=1`, plus `LAYA_SLOW_BASE` (a fine-tuned Laya folder, for the training tests) or `LAYA_SLOW_MODEL` (a
questions checkpoint folder); without them they are skipped.

## License

MIT, see [LICENSE](LICENSE).
