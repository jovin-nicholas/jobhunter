# Training your own Laya

jobhunter scores with Laya, a local decision model. The base model (`convaiinnovations/laya`) works as it is; a
checkpoint fine-tuned on your own job decisions fits your taste better. This folder trains one on a Colab GPU.

## Checkpoint methods

A checkpoint folder's `rl_agent_config.json` says how its decisions were trained, and jobhunter reads it to know how
to ask. A Hugging Face id uses the `question` method.

- **`question` and `from_score`** give a 1-10 score, and `decisions:` in `jobhunter.yaml` decides from it. For
  `from_score` the checkpoint's `score_thresholds` only choose how its answer is turned into that score.
- **`alert`** answers one yes/no question (is this job worth an alert?) and decides alert, save for later or skip
  itself from its `alert_at` / `save_at` cut-offs. Results show as "fit NN%".
- **`questions`** (the current method) asks three questions: the job's stack role and whether it states a dealbreaker,
  both from the posting alone, then, only when the role is one of its `stack_roles` and the dealbreaker chance is below
  `dealbreaker_at`, how good a fit the job is (1-10). A job that fails a gate is skipped, and the reason names the
  gate. The fit is summarised as the expected score (`expected`) or the chance of a 7-10 (`p_good`) and compared with
  `alert_at` / `save_at`. Results show as "fit 6.42/10" (or "fit NN%" for `p_good`).

The alert and questions cut-offs and gates can be overridden in `jobhunter.yaml` ([docs/settings.md](../docs/settings.md#scorers-required)).
`check-config` prints the ones in use and where each comes from.

## Fine-tuning

Use `train_questions.ipynb`, which trains a questions checkpoint; `train_alert.ipynb` trains the older alert
checkpoint. Dependencies are in `requirements-train.txt` (which includes `requirements-laya.txt`).

1. Prepare six files: `laya_train.py`, `jobhunter/posting.py`, your resume as plain text named `scoring_resume.txt`,
   two pools of jobs you labelled (`pool_old.jsonl` and `pool_jobhunter.jsonl`, for example older and recent labels),
   and `heldout_ids.txt` (job ids kept out of training, separated by spaces or line breaks).
2. Open the notebook in Colab with a GPU, run the cells and upload the files when asked.
3. Read the report it prints, download the zip it writes and check it with `shasum -a 256 -c SHA256SUMS`.
4. Unzip it and point `laya.model` in `jobhunter.yaml` at the folder.

If Colab disconnects, rerun the setup and data cells; the restore cell picks up the best saved epoch.

A pool is JSON lines with `job_id`, `title`, `company`, `description`, `label` (`notify`, `log` or `skip`) and
optionally `location`. The questions method also needs `match_score` (1-10), `stack_role` and `dealbreaker` (text or
null) on every row. `jobhunter export-feedback` writes your verdicts from the alert buttons to a CSV; turning them into
a pool is a manual step today.
