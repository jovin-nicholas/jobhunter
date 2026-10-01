# jobhunter settings reference

Everything goes in `jobhunter.yaml`. Only `resumes`, `search` and `scorers` are required; every other option below
has a default that suits most people. `check-config` reports every problem at once, with the key that caused it.
`examples/full.yaml` is a complete example that uses most options.

Secrets never go in this file: options ending in `_env` name an environment variable in `.env`.

## resumes (required)

| Key | Default | Meaning |
|---|---|---|
| `folder` | `resumes/` | Folder holding the files below, relative to this file |
| `default` | required | The resume used when no version matches; `.pdf`, `.docx`, `.txt` or `.md` |
| `versions` | none | Other resumes with `keywords`; a job mentioning more of a version's keywords gets that version |
| `names` | read from `CANDIDATE_NAMES` in `.env`, else the default resume's first line | Names removed before any model sees a resume |

## search (required)

| Key | Default | Meaning |
|---|---|---|
| `queries` | required | What to search for, e.g. `[backend engineer, SDE new grad]` |
| `locations` | none | Where; each query is searched in each location |
| `max_age_hours` | 72 | Older postings are ignored (and discovered ones remembered as `stale`) |
| `fetch_descriptions` | true | Download the posting page when a board gives no description |

## boards

Left out: all 12 built-in boards run. Given: exactly the listed boards run.

```yaml
boards: [dice, linkedin, greenhouse]            # names only
boards:                                          # or with options
  greenhouse: {companies: [stripe, airbnb]}      # whole company boards (turns discovery off unless discover: true)
  dice: {timeout_s: 300, enabled: false}
```

| Board | Options (defaults) | Notes |
|---|---|---|
| `dice` | `jobs_per_page: 100`, `max_pages: 3` | Full descriptions through Dice's public MCP server; newest first, only postings Dice dates within `max_age_hours` (1, 3 or 7 days) |
| `linkedin` | `date_since_posted: past 24 hours`, `max_hours_old: 1`, `limit: 25`, `delay_s: 5`, `npx` | Needs Node.js; skipped with a note without it. The search server gives only a posting date, so `max_hours_old` applies only when it also says "N hours ago"; `search.max_age_hours` still applies |
| `industry_jobs` | `tags: [jobs, hiring]` | dev.to job posts |
| `top_companies` | `amazon: true`, `amazon_queries: 4`, `greenhouse: {sofi: SoFi, stripe: Stripe}` | Amazon plus named Greenhouse boards |
| `hydepark` | `collection_id: 112`, `job_functions: [Software Engineering]` | Hyde Park Venture Partners portfolio |
| `greenhouse`, `lever`, `ashby` | `companies: []`, `discover` (on when no companies) | Company boards and discovered postings |
| `workday`, `dover`, `adp`, `gem` | `discover: true` | Discovered postings only |

Every board also takes `enabled` (true) and `timeout_s` (1800 for discovering ATS boards, 1200 for linkedin, 600 for
the others). A board in `plugins/` is listed the same way.

## discovery

Where boards with `discover` find postings.

| Key | Default | Meaning |
|---|---|---|
| `github_readmes` | three public new-grad and internship job lists | READMEs whose ATS links are read; `[]` turns this off |
| `google` | off | `{api_key_env: GOOGLE_API_KEY, cx_env: GOOGLE_CX}`: Google Custom Search, 100 free queries a day. Google closed this API to new customers and ends it on 2027-01-01, so only existing keys work, and only until then |

## filters

All optional; they run in this order before any model.

| Key | Meaning |
|---|---|
| `location.countries` | Keep jobs in these countries: `[US]`, or `{code, names, region_codes}` for one without built-in data (built in: US, CA, GB, IN, DE). A list ("Austin, TX; Toronto, ON") is kept when any place is allowed; a location naming no place ("In-Office") is read from the title, then the description |
| `seniority.levels` | Title levels to keep: `intern`, `entry`, `mid`, `senior`, `staff` (a title with no level word is kept) |
| `seniority.max_years_required` | Skip jobs asking for more years than this: "5+ years of … experience"; company history ("over 40 years of experience"), preferred years and years that stand in for a degree do not count |
| `keywords.exclude_title` | Skip when a title contains any of these words |
| `keywords.exclude_phrases` | Skip when the title or description contains any of these phrases |
| `keywords.exclude_companies` | Skip these companies |
| `keywords.exclude_roles` | Groups `{terms: [...], min_matches: N}`: skip when N different terms of a group appear |
| `systemone` | Off unless set. `{model: nimble:9b-q4_K_M}` (also `url`, `timeout_s: 30`): a local System One model (Ollama 0.35+) asks whether each kept job is a software engineering role, whether it is only for students (when `seniority.levels` leaves out `intern`), and where a job is when the location rules cannot tell. A model that does not answer is noted once in the run log and the job is kept. |

Matching is whole-word and case-insensitive.

## scorers (required)

Tried in order until one answers; rate limits and errors fall through to the next.

| Scorer | Options (defaults) |
|---|---|
| `laya` | `model` (a local checkpoint folder or a Hugging Face id), `device` (best available) |
| `ollama` | `model`, `url: http://localhost:11434`, `timeout_s: 240`, `max_description_chars: 2000` |
| `gemini` | `model: gemini-3.5-flash-lite`, `api_keys_env: GEMINI_API_KEYS`, `min_interval_s: 4`, `timeout_s: 30` |
| `groq` | `model: openai/gpt-oss-120b`, `api_keys_env: GROQ_API_KEYS`, `min_interval_s: 2.1` |

Several cloud keys can be listed comma-separated in `.env`; a rate-limited key rests while the others are used.

## decisions

| Key | Default | Meaning |
|---|---|---|
| `notify_at` | 7 | A score of at least this is notified |
| `log_at` | 5 | At least this (and below `notify_at`) is logged; lower is skipped |
| `min_confidence` | none | A notify whose scorer confidence (Laya reports one) is below this, from 0 to 1, is logged instead |

## schedule

Used by `scheduler.py` and `run --scheduled` (cron). A plain `run` ignores it.

| Key | Default | Meaning |
|---|---|---|
| `every` | `1h` | Time between runs: minutes or hours, `15m` to `24h` |
| `quiet_hours` | none | `"22:00-07:00"`: no runs in this window (may cross midnight) |
| `quiet_days` | none | `[sat, sun]`: no runs on these days |
| `timezone` | the computer's | For quiet hours and days, e.g. `America/New_York` |

## notify

Off unless set. A failed channel is logged and never stops a run.

```yaml
notify:
  slack: {webhook_env: SLACK_WEBHOOK_URL}
  email: {from_env: GMAIL_ADDRESS, password_env: GMAIL_APP_PASSWORD, to_env: NOTIFY_EMAIL}
```

## cover_letters

| Key | Default | Meaning |
|---|---|---|
| `enabled` | false | Add a draft letter to each notification |
| `writer` | none | A scorer listed under `scorers` that can write text: `ollama`, `gemini` or `groq` |
