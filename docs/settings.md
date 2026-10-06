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

Left out: all 13 built-in boards run. Given: exactly the listed boards run.

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
| `top_companies` | `amazon: true`, `amazon_queries: 4`, `greenhouse: {sofi: SoFi, stripe: Stripe}` | amazon.jobs, searched with the first `amazon_queries` search queries, plus whole Greenhouse boards: each key is the company's id from `boards.greenhouse.io/<id>`, each value the name to show. A `greenhouse` list replaces the default one |
| `hydepark` | `collection_id: 112`, `job_functions: [Software Engineering]`, `hits_per_page: 100` | Hyde Park Venture Partners portfolio |
| `greenhouse`, `lever`, `ashby` | `companies: []`, `discover` (on when no companies) | Company boards and discovered postings |
| `workday` | `discover: true` | Discovered postings only |
| `vc_boards` | none | Jobs from `discovery.getro` / `discovery.consider` that no ATS board above reads this run; nothing to set |
| `dover` | `job_board: true`, `companies: []`, `discover: true` | Dover's public feed of every company's jobs, plus the careers pages listed (`app.dover.com/<name>`) and discovered postings |
| `gem` | `companies: []`, `discover: true` | Gem boards listed (`jobs.gem.com/<name>`) and discovered postings |
| `adp` | `companies: []`, `discover: true` | ADP Workforce Now companies listed by their `cid=` (from a posting link), as a list or as `{<cid>: Company name}` so alerts show the name; plus discovered postings |

Every board also takes `enabled` (true) and `timeout_s` (1800 for the ATS boards greenhouse, lever, ashby, workday,
dover, adp and gem whether or not they discover, 1200 for linkedin, 900 for vc_boards, 600 for the others). A board in `plugins/` is listed the same way.

## discovery

Where boards with `discover` find postings.

| Key | Default | Meaning |
|---|---|---|
| `github_readmes` | three public new-grad and internship job lists | READMEs whose ATS links are read; `[]` turns this off |
| `google` | off | `{api_key_env: GOOGLE_API_KEY, cx_env: GOOGLE_CX}`: Google Custom Search, 100 free queries a day. Google closed this API to new customers and ends it on 2027-01-01, so only existing keys work, and only until then |
| `getro` | off | `{collections: {189: Redpoint, 1124: Primary}, job_functions: [Software Engineering], locations: [United States], seniority: [], max_pages: 10}`: VC portfolio job boards on Getro (the id is the board's collection number). Newest first; a board stops at an empty page, the first page past `max_age_hours`, or a page with nothing new. Jobs cached by an earlier run count as known only once a run has read that board to one of those stops; after a failed page or `max_pages`, the next run pages past them |
| `consider` | off | `{boards: {jobs.a16z.com: a16z}, roles: [software-engineer]}`: VC portfolio job boards on Consider (a16z; Sequoia, Lightspeed and others use the same system). The 25 newest jobs per role |

Jobs from `getro` and `consider` whose link is a Greenhouse, Lever, Ashby, Workday, Gem, Dover or ADP posting go to that board when it runs this time with `discover` on, and it reads the full posting. Every other job comes from the `vc_boards` board, including an ATS link whose board is off, has `discover` off (e.g. `greenhouse` with only `companies`), is missing from an explicit `boards:` list, or is left out by `--only`; `vc_boards` logs how many links it handed over and kept. An explicit `boards:` list must include `vc_boards` to get these jobs. A posting listed by two VC boards, under the same link, is one job. `vc_boards` reads each description from the posting page; when the page gives no description (under 200 characters), the job is tried again on later runs rather than judged by its title, and the board's summary counts it as "no description". Listings from the last `max_age_hours` (an undated one from when it was first seen) are kept in `data/vc_listings.json` between runs; `--dry-run` neither reads nor writes that file, so a dry run reads every board from its first page. Consider boards are read from data inside their web page; when a page no longer has it (Consider changed its page), `vc_boards`' summary line ends with `PROBLEM:` naming the board.

## filters

All optional; they run in this order before any model.

| Key | Meaning |
|---|---|
| `location.countries` | Keep jobs in these countries: `[US]`, or `{code, names, region_codes}` for one without built-in data (built in: US, CA, GB, IN, DE). A list ("Austin, TX; Toronto, ON") is kept when any place is allowed; an empty location, or one naming no place ("In-Office"), is read from the title, then the description; still undecided, it goes to `systemone` when set |
| `seniority.levels` | Title levels to keep: `intern`, `entry`, `mid`, `senior`, `staff` (a title with no level word is kept) |
| `seniority.max_years_required` | Skip jobs asking for more years than this: "5+ years of … experience", "8+ years of Java", "5 years of Python" on a bullet or under a requirements heading, "2 years required", "Years of experience: 4"; company history ("The team has built software for 15 years") ("over 40 years of experience"), preferred years and years that stand in for a degree do not count. Lines joined by a lone "OR" line are alternatives, so the lowest applies; "typically 5 years" is not a requirement unless a word such as "requires" comes first; "3 5 years" (a range that lost its dash) reads as 3 |
| `keywords.exclude_title` | Skip when a title contains any of these words |
| `keywords.exclude_phrases` | Skip when the title or description contains any of these phrases |
| `keywords.exclude_companies` | Skip these companies |
| `keywords.exclude_roles` | Groups `{terms: [...], min_matches: N}`: skip when N different terms of a group appear |
| `employment.exclude` | `[contract, hourly]`: either or both, required. No board reports employment type, so this reads the title and description for contract or hourly-pay wording instead |
| `employment.contract_to_hire` | `keep` (default) or `exclude`: whether "contract to hire", "cth" and "temp to perm"/"temp to hire" wording skips a job on their own |
| `employment.hourly_internships` | `keep` (default) or `exclude`: whether an hourly rate skips a job whose title says intern, internship or co-op |
| `systemone` | Off unless set. `{model: nimble:9b-q4_K_M}` (also `url`, `timeout_s: 30`, `skip_if: []`): a local System One model (Ollama 0.35+). See below. |

Matching is whole-word and case-insensitive. `employment` reads:

- **In the title only:** "contract", "contractor", "temporary", "freelance", "c2c", "corp to corp", "w2 contract", and
  "temp" only as a label ("Software Engineer (Temp)", "- Temp") or before role, position, assignment, job, worker or
  contract, so "Temp Sensor Firmware Engineer" is kept. "Smart contract(s)" never counts.
- **In the title or description:** "contract role / position / opportunity / assignment", "contractor role / position",
  "as a contractor", "independent contractor", "c2c", "corp to corp", "w2 contract", "W2 only" / "only W2", "1099"
  only as "1099 contract", "on 1099", "1099 only", "1099 or c2c" / "c2c or 1099" (a bare "1099", as in "Form 1099
  processing", is kept), "6-month contract", "Duration: contract" and "Duration: 6 months" / "12+ Months" (a
  duration followed within 4 words by program, onboarding, training, rotation, fellowship, residency, apprenticeship
  or internship, such as "Duration: 12 months onboarding program", is kept) and "employment type: contract".
- **Hourly pay:** a dollar amount per hour: "$55/hr", "$50-60 per hour", "Rate: $70.23/hr", "hourly rate of $40",
  "$40 hourly", "USD 40 per hour", "40 USD per hour". A number without a currency ("10,000/hr transactions", "500 per
  hour SLA", "PTO accrues at 1.54 per hour") in the description is not pay; in the title any number per hour is
  ("Data Engineer 65/hr W2").

The bare word "contracts" ("vendor contracts") and "subcontractor" or "contractors we work with" never skip.

### systemone

A local decision model answers questions the rules cannot. Each question is its own request about the job only;
nothing from your resume is sent.

- **Location**: asked inside the location filter, which runs first, and only when the rules cannot tell where a job
  is ("In-Office", a city the built-in data does not know). If the model does not answer, the rules' result stands,
  so such a job is still filtered.
- **Software role**: asked after the other filters for every job they kept: is this a software engineering role?
- **Students only**: asked when `seniority.levels` is set and leaves out `intern`: is the job only for current
  students?
- **`skip_if`**: any of `citizenship` and `clearance`. Asks whether the job requires citizenship of a
  `location.countries` country, or a security clearance, reading only the lines that mention eligibility or
  requirements. Equal-opportunity text, work authorisation and sponsorship policies do not count.

A job is skipped when the model's answer is yes with probability 0.5 or more. For the software-role, students and
`skip_if` questions, a model that does not answer keeps the job. Identical questions are answered from a cache. After
3 failures in a row the model is not asked again for the rest of the run, and the run log says so once. The model
stays loaded for 5 minutes after its last question.

## scorers (required)

Tried in order until one answers; rate limits and errors fall through to the next.

| Scorer | Options (defaults) |
|---|---|
| `laya` | `model` (a Hugging Face id, downloaded on first use, or a fine-tuned checkpoint folder), `device` (best available); for a checkpoint trained on the alert question, `alert_at` and `save_at` override its stored cut-offs (0-1, `save_at` below `alert_at`; any override that breaks `0 < save_at < alert_at < 1` is a settings error, including `alert_at` without `save_at` on a checkpoint that stores none). For a checkpoint trained on the questions method, `stack_roles` (the roles that pass the role gate, from backend, frontend, fullstack, ai, data, infra, other), `dealbreaker_at` (skip when the dealbreaker chance is at least this, above 0 and at most 1), `summary` (`expected`, a 1-10 score, or `p_good`, the chance of a 7-10) and `alert_at` / `save_at` (1-10 for `expected`, 0-1 for `p_good`, `save_at` below `alert_at`) override the checkpoint's; changing `summary` needs both `alert_at` and `save_at`, and an unknown role or out-of-range value is a settings error. Setting `alert_at` or `save_at` on a checkpoint trained on neither the alert question nor the questions method, or the questions options on any other checkpoint, is also a settings error |
| `ollama` | `model` (any pulled Ollama model; see the README's Ollama section), `url: http://localhost:11434`, `timeout_s: 240`, `max_description_chars: 2000`, `think: false` (thinking models answer about 4x faster), `temperature: 0` (the same posting always gets the same score), `num_ctx` (Ollama's default) |
| `gemini` | `model: gemini-3.5-flash-lite`, `api_keys_env: GEMINI_API_KEYS`, `min_interval_s: 4`, `timeout_s: 30`, `max_description_chars: 3000` |
| `groq` | `model: openai/gpt-oss-120b`, `api_keys_env: GROQ_API_KEYS`, `min_interval_s: 2.1`, `timeout_s: 30`, `max_description_chars: 3000` |

Several cloud keys can be listed comma-separated in `.env`; a rate-limited key rests while the others are used.

## decisions

| Key | Default | Meaning |
|---|---|---|
| `notify_at` | 7 | A score of at least this is notified |
| `log_at` | 5 | At least this (and below `notify_at`) is logged; lower is skipped |
| `min_confidence` | none | Laya's `question` and `from_score` methods also report how sure they are, from 0 to 1. A job that reaches `notify_at` but whose confidence is below this is logged instead of sent. Scorers without a confidence (Ollama, Gemini, Groq) and Laya's `alert` and `questions` methods are not affected |

A Laya checkpoint trained on the alert question or the questions method decides itself, from its own cut-offs
(`alert_at` / `save_at`, and the questions method's gates); `notify_at`, `log_at` and `min_confidence` do not apply to
it. They decide for every scorer that returns a 1-10 score, including Laya's older `question` and `from_score`
methods: for `from_score` the checkpoint's `score_thresholds` only choose how its answer is banded into a 1-10 score,
and `decisions:` then decides.

## schedule

Used by `scheduler.py` and `run --scheduled` (cron). A plain `run` ignores it.

| Key | Default | Meaning |
|---|---|---|
| `every` | `1h` | Time between runs: minutes or hours, `15m` to `24h` |
| `quiet_hours` | none | `"22:00-07:00"`: no runs in this window (may cross midnight) |
| `quiet_days` | none | `[sat, sun]`: no runs on these days |
| `timezone` | the computer's | For quiet hours and days, e.g. `America/New_York` |

## notify

Off unless set. A failed channel is logged and never stops a run. When no channel delivers an alert, the job is
saved as `error_notify` and tried again on later runs, for up to 24 hours after it was first saved.

```yaml
notify:
  slack: {webhook_env: SLACK_WEBHOOK_URL}
  email: {from_env: GMAIL_ADDRESS, password_env: GMAIL_APP_PASSWORD, to_env: NOTIFY_EMAIL}
```

`email` needs `from_env` and `password_env` (a Gmail app password). `to_env` is optional: when it is left out, alerts
go to the sending account's plus address `you+<alert_tag>@gmail.com`.

| Key | Default | Meaning |
|---|---|---|
| `to_env` | none | Where alerts go. Left out: the alert plus address below |
| `alert_tag` | `jobhunter` | Plus tag for alerts, so a Gmail filter can label them |
| `feedback_tag` | `jobhunter-feedback` | Plus tag the feedback buttons send to; must differ from `alert_tag` |

Tags may only contain letters, digits, `-` and `_`.

Each alert email has four buttons: ✅ Applied, 👍 Good, 🤷 Maybe and 👎 Bad match. A button opens a pre-filled email to
`you+<feedback_tag>@gmail.com` on the sending account; send it. On the next run jobhunter reads it over IMAP with the
same app password, saves the verdict in `data/jobs.db`, and moves the message out of the inbox under the label
`jobhunter/feedback`. Only messages from the sending account, or from the `to_env` address, count, and when Gmail's
own `Authentication-Results` header is there it must show `dkim=pass`, `spf=pass` or `dmarc=pass` for that
address's domain or a subdomain of it (a message it marks as failing is ignored, and the run log counts them). A message Gmail added no such header to (one you sent yourself may have none)
is trusted by its From address alone, and the run log says how many there were. `--dry-run` never reads the inbox,
and a problem reading it is logged and never stops alerts. `jobhunter export-feedback` writes each job's latest verdict
to `data/feedback_labels.csv` (or `--out`), with the decision it stands for: Applied and Good become notify, Maybe log,
Bad match skip. The CSV is a record of your verdicts; turning it into training pools is a manual step today (this repo
has no converter).

## cover_letters

| Key | Default | Meaning |
|---|---|---|
| `enabled` | false | Add a draft letter to each notification |
| `writer` | none | A scorer listed under `scorers` that can write text: `ollama`, `gemini` or `groq` |
| `options` | none | The writer's options for letters only, over its scorer options, e.g. `{model: qwen3:8b, temperature: 0.5}`. With `ollama`, letters default to `think: true`, `temperature: 0.7` and at least 600 s (`timeout_s`, or the scorer's own if longer), since thinking reads better and is slower; scoring keeps `think: false` and `temperature: 0`. A `gemini` or `groq` writer with options shares the scorer's keys and rate limit. A 4B model is enough to judge jobs but writes weak letters, so `options: {model: ...}` can give letters a larger model (README: Ollama). A model without thinking is asked again without it, so any Ollama model works |
