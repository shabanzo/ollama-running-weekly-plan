#!/usr/bin/env python3
"""Local web app for the weekly training plan.

Flow: edit training settings -> Generate (fetches Intervals.icu, asks the local
Ollama model, validates) -> review the plan on screen -> Submit pushes it to
your Intervals.icu calendar. Everything runs on your machine; nothing is sent
anywhere except your own Intervals.icu account and your local Ollama server.

Run:
    python3 app.py
Then open http://127.0.0.1:5050
"""
import datetime as dt
import json
import os
import pathlib
import queue
import threading

from flask import Flask, Response, redirect, render_template, request, url_for

import plan_engine as pe

APP_DIR = pathlib.Path(__file__).resolve().parent
SETTINGS_FILE = APP_DIR / "settings.json"

DEFAULT_SETTINGS = {
    "race_date": "2026-11-08",
    "race_km": "10",
    "goal_time": "59:30",
    "result_km": "10",
    "result_time": "60:00",
    "sport_notes": "Runs 4-5 times per week. Long run on the weekend. Max 60 min on weekdays.",
}


def load_env(path=".env"):
    """Credentials and machine settings (athlete id, API key, model, Ollama URL,
    output folder) stay in .env, not in the web form, since they rarely change
    and the API key shouldn't round-trip through a browser form."""
    p = APP_DIR / path
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        s.update(json.loads(SETTINGS_FILE.read_text()))
    return s


def save_settings(s):
    SETTINGS_FILE.write_text(json.dumps(s, indent=2))


def build_cfg(settings):
    return {
        "athlete_id": os.environ.get("INTERVALS_ATHLETE_ID", ""),
        "api_key": os.environ.get("INTERVALS_API_KEY", ""),
        "model": os.environ.get("MODEL", "qwen3:30b"),
        "ollama_url": os.environ.get("OLLAMA_URL", "http://localhost:11434/api/chat"),
        "out_dir": pathlib.Path(os.environ.get("OUT_DIR", str(APP_DIR / "plans"))),
        "max_tries": int(os.environ.get("MAX_TRIES", "3")),
        "race_date": dt.date.fromisoformat(settings["race_date"]),
        "race_km": float(settings["race_km"]),
        "goal_time": settings["goal_time"].strip(),
        "result_km": float(settings["result_km"]),
        "result_time": settings["result_time"].strip(),
        "sport_notes": settings["sport_notes"].strip(),
    }


app = Flask(__name__)

# Single-user local app: one in-memory slot for the last generated plan is
# simpler and safer than a database. It's lost on restart; Submit right after
# Generate, or just regenerate (a few minutes on CPU).
STATE = {
    "result": None,
    "log": [],
    "events": None,
    # Generation progress: None = idle, Queue = running, "done"/"error" = finished.
    "gen_queue": None,
    "gen_lock": threading.Lock(),
}


# ----------------------------- background worker -----------------------------

def _run_generation(cfg, q: queue.Queue):
    """Runs in a daemon thread. Puts SSE-ready strings into q.
    Sentinel: "done" or "error:<message>".
    """
    def push(msg):
        q.put(f"log:{msg}")

    try:
        result = pe.generate_plan(cfg, on_progress=push)
    except pe.GenerationError as e:
        q.put(f"error:{e}")
        return
    except Exception as e:
        q.put(f"error:Unexpected error: {e}")
        return

    if result.get("post_race"):
        q.put("error:Race date has passed. Update Race date in settings to plan the next block.")
        return

    try:
        pe.save_plan(result, cfg["out_dir"])
    except Exception as e:
        push(f"Warning: could not save plan to disk: {e}")

    STATE["result"] = result
    STATE["events"] = pe.build_intervals_events(result)
    STATE["log"] = ["Plan generated. Review it below, then Submit when you're happy with it."]
    q.put("done")


# ---------------------------------- routes ----------------------------------

@app.route("/", methods=["GET"])
def index():
    settings = load_settings()
    creds_missing = (not os.environ.get("INTERVALS_ATHLETE_ID")
                      or not os.environ.get("INTERVALS_API_KEY")
                      or os.environ.get("INTERVALS_API_KEY") == "your_key")
    return render_template(
        "index.html",
        settings=settings,
        result=STATE["result"],
        log=STATE["log"],
        events=STATE["events"],
        events_json=json.dumps(STATE["events"], indent=2) if STATE["events"] else None,
        creds_missing=creds_missing,
    )


@app.route("/settings", methods=["POST"])
def update_settings():
    s = {
        "race_date": request.form["race_date"],
        "race_km": request.form["race_km"],
        "goal_time": request.form["goal_time"],
        "result_km": request.form["result_km"],
        "result_time": request.form["result_time"],
        "sport_notes": request.form["sport_notes"],
    }
    save_settings(s)
    STATE["log"] = ["Settings saved."]
    return redirect(url_for("index"))


@app.route("/generate", methods=["POST"])
def generate():
    """Validates config, then kicks off generation in a background thread.
    The browser immediately redirects to / where the Generate button is replaced
    by the live stream UI (which connects to /stream via SSE).
    """
    settings = load_settings()
    try:
        cfg = build_cfg(settings)
    except ValueError as e:
        STATE["result"], STATE["events"] = None, None
        STATE["log"] = [f"Check your settings: {e}"]
        return redirect(url_for("index"))

    if not cfg["athlete_id"] or not cfg["api_key"] or cfg["api_key"] == "your_key":
        STATE["result"], STATE["events"] = None, None
        STATE["log"] = ["Set INTERVALS_ATHLETE_ID and INTERVALS_API_KEY in .env next to app.py, then restart."]
        return redirect(url_for("index"))

    with STATE["gen_lock"]:
        if isinstance(STATE["gen_queue"], queue.Queue):
            # Already running — ignore duplicate clicks.
            return redirect(url_for("index"))
        q = queue.Queue()
        STATE["gen_queue"] = q
        STATE["result"] = None
        STATE["events"] = None
        STATE["log"] = []

    t = threading.Thread(target=_run_generation, args=(cfg, q), daemon=True)
    t.start()
    return redirect(url_for("index"))


@app.route("/stream")
def stream():
    """Server-Sent Events endpoint. Drains the generation queue and forwards
    events to the browser until a 'done' or 'error:...' sentinel arrives.
    """
    def event_stream():
        q = STATE.get("gen_queue")
        if not isinstance(q, queue.Queue):
            # Nothing is running — tell the client immediately.
            yield "data: done\n\n"
            return
        while True:
            try:
                msg = q.get(timeout=30)
            except queue.Empty:
                # Send a keep-alive comment so the connection stays open.
                yield ": ping\n\n"
                continue
            yield f"data: {msg}\n\n"
            if msg == "done" or msg.startswith("error:"):
                with STATE["gen_lock"]:
                    STATE["gen_queue"] = None
                break

    return Response(event_stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/submit", methods=["POST"])
def submit():
    settings = load_settings()
    result = STATE["result"]
    if not result:
        STATE["log"] = ["Generate a plan first."]
        return redirect(url_for("index"))
    try:
        cfg = build_cfg(settings)
        events = STATE["events"] or pe.build_intervals_events(result)
        pe.submit_to_intervals(cfg, events)
        STATE["log"] = [f"Submitted {len(events)} workouts to Intervals.icu for the week of {result['plan_start']}. "
                         "Submitting again will update the same events, not duplicate them."]
    except Exception as e:
        STATE["log"] = [f"Submit failed: {e}"]
    return redirect(url_for("index"))


if __name__ == "__main__":
    # threaded=True is required so /stream and other routes can be served
    # simultaneously while the generation thread is running.
    app.run(host="127.0.0.1", port=5050, debug=True, use_reloader=False, threaded=True)
