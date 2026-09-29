# Weekly Training Plan — local web app

A small local Flask app that replaces the cron job:

1. Edit race/fitness settings on the page (saved to `settings.json`).
2. Click **Generate plan** — it reads last week + the week before from
   Intervals.icu, asks your local Ollama model for a plan, validates it
   (long-run cap, one quality session, etc.), retrying up to 3 times.
3. **Review** the plan and its warnings on screen.
4. Click **Submit this plan to Intervals.icu** to push it to your calendar.
   Submitting again for the same week updates the same events instead of
   duplicating them.

Nothing leaves your machine except calls to your own Intervals.icu account
and your local Ollama server.

## Setup

```bash
cd /Users/shabanzo/Projects/running   # or wherever you keep this
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env: set INTERVALS_ATHLETE_ID and INTERVALS_API_KEY
```

## Run

```bash
source venv/bin/activate
python3 app.py
```

Open http://127.0.0.1:5050

Leave the terminal running while you use the page. Generating a plan on a
CPU-only Mac can take several minutes; the page just waits, so don't close
the tab.

## Files

- `app.py` — Flask routes and the settings/`.env` glue.
- `plan_engine.py` — all the actual logic (VDOT paces, phase rules, the
  validator, Intervals.icu calls, the Ollama call, the Intervals.icu event
  builder). No printing or CLI here, so it's reusable.
- `templates/index.html` — the one page: settings form, Generate button,
  plan review table, Submit button.
- `settings.json` — created automatically the first time you save settings.
  Holds race date, race distance, goal time, your latest test result, and
  your constraints text.
- `.env` — credentials and machine settings (athlete ID, API key, model
  name, Ollama URL, output folder). Kept out of the web form on purpose,
  since it holds your API key. Not committed to git — see `.gitignore`.
- `plans/` — `plan-<date>.md` and `.json` are saved here each time you
  generate. The `.json` file is what next week's adherence check reads, so
  don't delete recent ones.

## What gets pushed to Intervals.icu

Each non-rest day in the plan becomes one calendar event:

```json
{
  "category": "WORKOUT",
  "start_date_local": "2026-10-06T06:00:00",
  "type": "Run",
  "name": "Threshold",
  "description": "3x5min T \u2014 15 min at pace",
  "moving_time": 2700,
  "external_id": "weekly-plan-2026-10-05-2026-10-06"
}
```

It uses Intervals.icu's bulk upsert endpoint
(`POST /api/v1/athlete/{id}/events/bulk?upsert=true`) with a stable
`external_id` per day, so re-submitting the same week updates those events
rather than creating duplicates. Rest days aren't pushed as events.

This event shape is based on Intervals.icu's own forum guide on uploading
planned workouts. It's the plain `description`-text form (not the
structured `workout_doc` used for power-based interval targets), which is
right for a pace-based running plan like this one. Do one test submit and
check it shows up correctly on your calendar before trusting a full week —
Intervals.icu's API details can change.

## Notes

- This is a single-user local tool: one in-memory slot holds the
  "last generated plan," so opening two browser tabs, or restarting the
  app between Generate and Submit, will lose it — just regenerate.
- Credentials live in `.env`, not the web form or `settings.json`, and
  `.env` is not sent anywhere by the app itself.
- The "Preview what will be sent" section on the review page shows the
  exact JSON before you submit.
