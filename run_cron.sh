#!/bin/bash
# One scheduled jobhunter run from this folder's virtualenv. Point cron at it every 15 minutes:
#   */15 * * * * /path/to/jobhunter/run_cron.sh
# jobhunter decides from schedule.every and the quiet hours/days in jobhunter.yaml whether a run is due.
cd "$(dirname "$0")" || exit 1
mkdir -p logs
exec .venv/bin/python -u -m jobhunter run --scheduled >> logs/cron.log 2>&1
