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
.venv/bin/python -m jobhunter check-config
.venv/bin/python -m jobhunter run --dry-run                           # scores everything, saves and sends nothing
.venv/bin/python -m jobhunter run
```

`jobhunter.example.yaml` holds only the choices you have to make; everything else has a default that suits most
people. Every option, with its default, is in [docs/settings.md](docs/settings.md), and
[examples/full.yaml](examples/full.yaml) is a complete example.

The first run takes longest: discovery finds about 1,500 ATS job links in the public job lists, and each posting is
downloaded once. Postings older than `search.max_age_hours` (status `stale`) and postings that no longer exist
(status `gone`) are then remembered and skipped. A dry run remembers nothing, so every dry run downloads them again.

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
loads the model, about 5 s).

- `laya.model` is a local checkpoint folder, such as the fine-tuned one from the Colab notebook
  (`~/models/laya_finetuned`), or a Hugging Face id such as `convaiinnovations/laya`.
- A local folder's `rl_agent_config.json` says how its decisions were trained (`question` or `from_score`) and at
  which thresholds; jobhunter reads both. For a Hugging Face id the defaults are used (`question`, notify at 7, log
  at 5).
- If Laya is not installed or its model is missing, jobs fall through to the next scorer (`ollama`).
- A job whose description could not be downloaded (the site refused or timed out) is not scored from its title; it
  is saved as `error_unavailable` and tried again on the next run.

### Optional: a System One checker

A local decision model can answer the questions the filters cannot: is this a software engineering role, is it only for
current students, where is a job whose location names no place, and (optionally) does it require citizenship or a
security clearance. Install Ollama 0.35 or later, run
`ollama pull nimble:9b-q4_K_M` (5.6 GB), and add one line under `filters:`:

```yaml
  systemone: {model: "nimble:9b-q4_K_M", skip_if: [citizenship, clearance]}   # skip_if is optional
```

Each question is a separate request about the job only (nothing from your resume is sent), about 3-5 seconds each
on an Apple-silicon Mac. The location question is asked only when the location rules cannot decide; the others only
for jobs the other filters kept. If the model is missing or slow, the run log says so once and jobs go on to the
scorers as usual. Details are in [docs/settings.md](docs/settings.md#systemone).

## Boards

All 12 built-in boards run unless `boards:` lists others: `dice`, `linkedin`, `industry_jobs` (dev.to),
`top_companies` (Amazon, SoFi, Stripe), `hydepark`, and the ATS boards `greenhouse`, `lever`, `ashby`, `workday`,
`dover`, `adp` and `gem`, which read postings found by discovery in the public job lists. Greenhouse, Lever and Ashby
can also list whole company boards: `greenhouse: {companies: [stripe, airbnb]}`.

- **linkedin** searches through the `linkedin-jobs-mcp` server, which needs Node.js (`npx`; the first run downloads
  it). Without Node.js the board is skipped with a note. Descriptions come from LinkedIn's public job-posting pages,
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
runs. A board class may set `default_timeout_s`, and may raise `BoardSkipped` when it cannot run on this machine.

Scorers work the same way with `@scorer("name")` and a `score(job, resume)` method returning a `ScoreResult`. A scorer
that also has `generate(prompt) -> str` can write cover letters.

## Commands

| Command | What it does |
|---|---|
| `run [--only dice,greenhouse] [--dry-run] [--scheduled]` | Search, filter, score, save and notify; `--dry-run` saves and sends nothing; `--scheduled` honours `schedule:` |
| `check-config` | Validate settings, plugins and resumes, then exit |
| `list-boards`, `list-scorers` | Built-in and plugin boards and scorers, and where each comes from |
| `import-db PATH` | Copy jobs from a job-notifier database so none is scored or notified again |
| `compare-db PATH [--since YYYY-MM-DD]` | For jobs both apps decided: how often they agree, and where they differ |

Each job is saved in `data/jobs.db` with a status: `notified`, `logged` or `skipped` (the score against
`decisions`), `filtered` (with the filter's reason), `stale` or `gone` (a discovered posting too old, or no longer
there), `duplicate` (would have been notified, but the same job was already notified in the last 30 days or earlier in
the run), `error_429_retry` (scored again when a board lists it again), `error_unavailable` / `error_notify` (tried again on
later runs for up to 24 hours after first being saved; `error_notify` means no Slack or email channel delivered the
alert), or `error_terminal` (no scorer could read its answer for this job; not retried).

A job is notified once: the same title at the same company counts as one job whatever board, id or location it comes
with.

## Moving from job-notifier

1. Copy your resumes into `resumes/` and your `.env`; start from `examples/full.yaml`; run `check-config`.
2. Run jobhunter for a day next to job-notifier with notifications off (leave out `notify:`), so both see the same jobs.
3. `python -m jobhunter compare-db ../job-notifier/data/jobs.db` shows how often both made the same decision, and the
   jobs where they differ. Compare before importing: after step 4 the imported jobs match themselves.
4. `python -m jobhunter import-db ../job-notifier/data/jobs.db` copies every job job-notifier already handled, so
   none is notified twice. A job jobhunter had left for retry takes job-notifier's final status. The old database is
   opened read-only and not changed; SQLite may leave empty `jobs.db-wal` / `jobs.db-shm` files next to it.
5. Turn notifications on and switch the cron entry to `run_cron.sh`.

## Known limitations

- The same posting found on two boards (for example Stripe through `top_companies` and through `greenhouse`) is two
  jobs, and Workday job ids do not include the company; both keep job ids identical to job-notifier's.
- A site's DNS answer is checked before a page download but could change in between; for a single-user tool this is
  accepted.
- Jobs are scored one at a time.

## Using job sites responsibly

jobhunter reads public job listings and posting pages, spaced per site and with backoff when a site asks it to slow
down. You are responsible for following each site's terms of use; turn off any board you should not use with
`boards:` in `jobhunter.yaml`.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## License

MIT, see [LICENSE](LICENSE).
