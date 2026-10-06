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

- `laya.model` is a Hugging Face id such as `convaiinnovations/laya`, downloaded on the first run, or the folder of a
  Laya you fine-tuned on your own job decisions (`~/models/laya_finetuned`).
- A local folder's `rl_agent_config.json` says how its decisions were trained (`question`, `from_score`, `alert` or
  `questions`). The two older methods give a 1-10 score and `decisions:` in `jobhunter.yaml` decides from it; for
  `from_score` the checkpoint's `score_thresholds` only choose how its answer is turned into that score. A Hugging
  Face id uses the `question` method.
- An alert checkpoint is fine-tuned on a single yes/no question (is this job worth an alert?) instead of a 1-10
  score; it decides alert, save for later or skip itself, from its own `alert_at` / `save_at` cut-offs (see
  `docs/settings.md`).
- Alerts, run logs and `compare-db` show an alert checkpoint's result as "fit NN%" rather than a 1-10 score.
- A questions checkpoint asks three questions instead: the job's stack role and whether it states a dealbreaker (on
  the posting alone), then, only when the role is one of its `stack_roles` and the dealbreaker chance is below
  `dealbreaker_at`, how good a fit the job is (1-10). A job that fails a gate is skipped, and the reason names the
  gate. The fit is summarised as the expected score (`expected`) or the chance of a 7-10 (`p_good`), and compared
  with the checkpoint's `alert_at` / `save_at`. All five can be overridden in `jobhunter.yaml` (see
  `docs/settings.md`). Alerts and run logs show its result as "fit 6.42/10" (or "fit NN%" for `p_good`), with the side of the cut-off in words.
- `check-config` prints the cut-offs an alert or questions checkpoint is using (and a questions checkpoint's gates),
  and whether they come from the checkpoint or were overridden in `jobhunter.yaml`.
- If Laya is not installed or its model folder is missing, jobs fall through to the next scorer (`ollama`). The run
  log notes it once per run, and `check-config` says what is missing.
- A job whose description could not be downloaded (the site refused or timed out) is not scored from its title; it
  is saved as `error_unavailable` and tried again on the next run.
- Fine-tuning your own checkpoint: [training/](training/) holds `laya_train.py` and two Colab notebooks, with
  dependencies in `requirements-train.txt` (which includes `requirements-laya.txt`). Use `train_questions.ipynb`: it
  trains a questions checkpoint (stack role, dealbreaker and fit); `train_alert.ipynb` trains the older
  single-question alert checkpoint. Both ask for six files: `laya_train.py`, `jobhunter/posting.py`, your resume as
  `scoring_resume.txt`, two pools of your own labelled jobs (`pool_old.jsonl`, `pool_jobhunter.jsonl`) and
  `heldout_ids.txt` (job ids kept out of training). A pool is JSON lines with `job_id`, `title`, `company`,
  `description`, `label` (notify, log or skip) and optionally `location`; the questions method also needs
  `match_score` (1-10), `stack_role` and `dealbreaker` (text or null) on every row. `export-feedback`'s CSV is a
  record of your verdicts, not a pool: building pools from it is a manual step today. Run the cells on a GPU, read
  the report it prints, then download the single zip it writes and check it with `shasum -a 256 -c SHA256SUMS`. If
  Colab disconnects, rerun the setup and data cells and the restore cell picks up the best saved epoch. Point
  `laya.model` at the unzipped folder.

## Ollama: choosing a local model

`gemma4:e4b` is only a suggestion. Any Ollama model that fits your computer can score jobs and write cover letters:
put its name in `ollama: {model: ...}` and run `ollama pull <name>`. Nothing is downloaded for you; `check-config`
says when a model is missing or Ollama is not running. A model needs:

- **About 4B parameters or more.** Smaller models often break the JSON answer or give every job the same score.
- **A context window of at least 4,096 tokens.** A prompt is about 1,300 tokens with a one-page resume and the first
  2,000 characters of a posting; raise `num_ctx` for a long resume.
- **Free memory of about its download size plus 1-2 GB.** As a guide: 8 GB of RAM fits a 4B model such as
  `qwen3:4b`; 16 GB fits `gemma4:e4b` (9.6 GB) or an 8B model such as `qwen3:8b`. Without a GPU (Apple silicon,
  or NVIDIA with enough memory) expect a minute or more per job.

`check-config` also warns when a model, or all the models in use together (Laya, the scorer, the cover-letter writer
and the System One model), need more memory than the computer can spare, and says how much disk space is free when a
model still has to be pulled. A pull that runs out of disk space fails and leaves the model missing; free some space
and pull it again.

**Judging jobs and writing cover letters need different sizes.** For judging jobs, a 4B model is enough: on 60 test
postings `qwen3:4b` (2.5 GB) picked good jobs as well as `gemma4:e4b` (9.6 GB), about 2.5 times faster. Cover
letters need a larger model: in the same test `qwen3:4b` repeated numbers from the resume against the prompt's rules,
reused one example twice and fell back on stock phrases, while `gemma4:e4b` followed the rules. So pick the scoring
model to fit your computer, and if you turn cover letters on, give them the largest model it can run:

```yaml
scorers:
  - ollama: {model: "qwen3:4b"}         # judges jobs
cover_letters:
  enabled: true
  writer: ollama
  options: {model: "gemma4:e4b"}        # writes the letters
```

Models with and without a thinking mode both work: scoring turns thinking off, and cover letters turn it on only when
the model supports it.

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

### Optional: skip contract and hourly jobs

No board says whether a job is a contract or paid by the hour, so the `employment` filter reads the title and
description for it, before any model runs:

```yaml
  employment:
    exclude: [contract, hourly]   # either or both
    contract_to_hire: keep        # or exclude
    hourly_internships: keep      # or exclude
```

Contract wording includes "contract", "contractor", "temporary" or "freelance" in the title, and "contractor role",
"independent contractor", "C2C", "1099 contract" / "1099 only" / "on 1099", "W2 only" / "only W2", "6-month contract"
and "employment type: contract" anywhere; a bare "1099" ("Form 1099 processing") and "smart contract" never count.
Hourly means a rate in dollars such as "$55/hr", "$50-60 per hour" or "USD 40 per hour" (not "the salary or hourly
rate offered", and not a number without a currency in the description, such as "10,000/hr transactions"; in the
title "65/hr" is enough). A skipped job is saved as `filtered` with the matched words as its reason, e.g. "contract: c2c". Details
are in [docs/settings.md](docs/settings.md).

## Boards

All 13 built-in boards run unless `boards:` lists others: `dice`, `linkedin`, `industry_jobs` (dev.to),
`top_companies` (Amazon, SoFi, Stripe), `hydepark`, and the ATS boards `greenhouse`, `lever`, `ashby`, `workday`,
`dover`, `adp` and `gem`, which read postings found by discovery in the public job lists. Greenhouse, Lever, Ashby,
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
first saved, and every scorer still failed on it; not retried), or `error` (processing the job crashed; tried again when a board lists it again).

A job is notified once: the same title at the same company counts as one job whatever board, id or location it comes
with.

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
