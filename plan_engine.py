"""Core training-plan logic: VDOT paces, phase rules, the JSON-plan validator,
and Intervals.icu I/O. No CLI, no printing — used by app.py (and could be used
by a CLI too). Everything takes explicit arguments; nothing is a module-level
global computed from config, so the web app can regenerate with new settings
without restarting.
"""
import datetime as dt
import json
import math

import requests

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
TYPES = ["easy", "long", "threshold", "interval", "race_pace",
         "fartlek", "rest", "cross", "race"]
QUALITY = {"threshold", "interval", "race_pace"}

# Intensity as a fraction of VO2max (approximations of the published Daniels
# intensities; compare the resulting paces with the VDOT tables in the book).
EASY_FRAC = (0.62, 0.70)
T_FRAC = (0.87, 0.89)
I_FRAC = (0.97, 0.99)

# Hard cap on long-run minutes by phase (the long run should build gradually).
LONG_CAP = {"build": 75, "specific": 90, "sharpen": 60, "taper": 0}
LONG_RUN_MAX_INCREASE = 15  # minutes vs last week's long run


class GenerationError(Exception):
    """Raised for anything that stops plan generation (Intervals.icu or Ollama failures)."""


# ---------------------- VDOT (Daniels-Gilbert equations) ----------------------
def _to_sec(t):
    sec = 0
    for part in t.split(":"):
        sec = sec * 60 + int(part)
    return sec


def _mmss(sec):
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def _hmmss(sec):
    sec = int(round(sec))
    h, r = divmod(sec, 3600)
    return f"{h}:{r // 60:02d}:{r % 60:02d}" if h else f"{r // 60}:{r % 60:02d}"


def vdot_from_result(km, sec):
    t = sec / 60
    v = km * 1000 / t                                   # m/min
    vo2 = -4.60 + 0.182258 * v + 0.000104 * v * v
    pct = 0.8 + 0.1894393 * math.exp(-0.012778 * t) + 0.2989558 * math.exp(-0.1932605 * t)
    return vo2 / pct


def pace_at(vdot, frac):
    """Pace in sec/km at a fraction of VO2max."""
    a, b, c = 0.000104, 0.182258, -(4.60 + vdot * frac)
    v = (-b + math.sqrt(b * b - 4 * a * c)) / (2 * a)   # m/min
    return 1000 / v * 60


def equiv_time(vdot, km):
    """Race time (sec) for a distance at the same VDOT (bisection)."""
    lo, hi = km * 60 * 2, km * 60 * 20
    for _ in range(60):
        mid = (lo + hi) / 2
        if vdot_from_result(km, mid) > vdot:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def build_paces(vdot, goal_time, race_km):
    def rng(frac):
        fast, slow = pace_at(vdot, max(frac)), pace_at(vdot, min(frac))
        return f"{_mmss(fast)}-{_mmss(slow)}/km"

    goal_pace = _to_sec(goal_time) / race_km
    return {
        "easy": rng(EASY_FRAC),
        "long": rng(EASY_FRAC),
        "threshold": rng(T_FRAC),
        "interval": rng(I_FRAC) + " (about 3K-5K effort)",
        "race_pace": f"{_mmss(goal_pace - 2)}-{_mmss(goal_pace + 2)}/km (goal {goal_time})",
        "strides": "20 sec fast but relaxed, walk/jog back",
    }


def phase_for(days_out):
    """Returns (phase_text_for_prompt_and_display, phase_key_for_logic)."""
    if days_out < 0:
        return "post-race", "post-race"
    if days_out <= 6:
        return (("taper + race week (final quality). Race is on " + DAYS[days_out] + ". Cut volume about 40%. "
                  "Mid-week: easy run + strides + 3-4 x 1 km at race pace with jog recovery. "
                  "Day before race: 20 min shakeout + 4 strides. Day before that: rest. No long run."),
                "taper")
    if days_out <= 13:
        return (("sharpen (late quality): volume about 20% below last week, one short session of race-pace "
                  "or I reps with generous recovery, short easy long run, long run at most "
                  f"{LONG_CAP['sharpen']} min"),
                "sharpen")
    if days_out <= 27:
        return (("specific (hardest block): the quality session is I-pace intervals (3-5 min bouts) or "
                  "race-pace reps, peak long run at easy pace, volume at most +10%, long run at most "
                  f"{LONG_CAP['specific']} min and at most {LONG_RUN_MAX_INCREASE} min longer than last week's"),
                "specific")
    return (("build (early quality): the quality session is T-pace work (cruise intervals or a steady tempo), "
             "plus strides, easy long run, volume at most +10%, long run at most "
             f"{LONG_CAP['build']} min and at most {LONG_RUN_MAX_INCREASE} min longer than last week's"),
            "build")


SYSTEM = """You are an experienced endurance running coach. Combine two ideas.

DANIELS-STYLE STRUCTURE
- Intensities: Easy (E), Threshold (T), Interval (I), race pace. Use only the paces provided.
- Quality ("Q") days are the hard days. The long run is also a Q day but is run at easy pace. Never put two hard days back to back, and never a hard session the day before the long run.
- T work: comfortably hard, steady or as cruise intervals (about 1 min jog per 5 min of T running). I work: bouts of 3-5 min at I pace with jog recoveries equal to or shorter than the bout. Race-pace work: reps of 1-3 km with short jog recoveries.
- Dose limits: T work per session at most about 10% of weekly distance; I work at most about 8% of weekly distance; long run at most about 25-30% of weekly volume and never over 150 min.
- work_minutes = minutes actually run at T, I or race pace (not warm-up, cool-down or recovery jogs). Warm-up and cool-down (10-15 min easy each) are part of "minutes". Set work_minutes to 0 on other days.
- Never fill the middle with medium-hard runs. Easy days stay truly easy.

GARMIN-STYLE ADAPTIVITY
- Adapt to the data: readiness, last week's plan vs actual, load trend.
- If readiness is low: no full quality session, strides only, and say why in the summary.
- If a key session was missed last week, do not stack it into this week; continue the phase. If less than about 70% of last week's planned minutes were completed, keep volume flat instead of raising it.
- Never raise volume and intensity in the same week.

RULES
- Every week has at least 1 speed session: strides or light fartlek in easy weeks, otherwise the quality session of the phase.
- At most 1 full quality session per week (threshold, interval or race_pace). One long run per week, except race week which has none.
- Follow the phase given in the data. Do not invent your own phase.
- Do NOT invent pace numbers. If data is missing, say so in the summary instead of guessing.
- If you state an adherence percentage in the summary, use exactly the "Completion" percentage given in
  the data. Do not calculate your own ratio (e.g. from a session count) and do not restate it differently.
- Do NOT state a percentage of minutes at quality pace, or any other statistic, unless you compute it
  exactly from the numbers given. If you are not certain a number is exactly right, describe it in words
  (e.g. "mostly easy volume, one quality session") instead of inventing a figure.
- Return JSON only, matching the schema. Details: one short line."""

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "array", "items": {"type": "string"}},
        "plan": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "day": {"type": "string", "enum": DAYS},
                    "type": {"type": "string", "enum": TYPES},
                    "minutes": {"type": "integer"},
                    "work_minutes": {"type": "integer"},
                    "strides": {"type": "boolean"},
                    "details": {"type": "string"},
                },
                "required": ["day", "type", "minutes", "work_minutes", "strides", "details"],
            },
        },
    },
    "required": ["summary", "plan"],
}


# ----------------------------- Intervals.icu I/O -----------------------------
def intervals_get(base, auth, path, oldest, newest):
    r = requests.get(f"{base}/{path}",
                     params={"oldest": oldest.isoformat(), "newest": newest.isoformat()},
                     auth=auth, timeout=30)
    r.raise_for_status()
    return r.json()


def week_stats(acts):
    return {
        "sessions": len(acts),
        "minutes": round(sum(a.get("moving_time") or 0 for a in acts) / 60),
        "km": round(sum(a.get("distance") or 0 for a in acts) / 1000, 1),
        "load": round(sum(a.get("icu_training_load") or 0 for a in acts)),
    }


FORM_FATIGUED = -15   # CTL-ATL below this is flagged as a fatigue signal
FORM_SEVERE = -25     # below this, readiness is "low" on its own, no second flag needed


def readiness(well):
    """Simple, transparent readiness heuristic from the last 7 days of wellness data."""
    flags, severe = [], False
    ca = [x for x in well if x.get("ctl") is not None and x.get("atl") is not None]
    if ca:
        form = ca[-1]["ctl"] - ca[-1]["atl"]
        if form < FORM_FATIGUED:
            flags.append(f"form {form:.0f} (fatigued)")
            if form < FORM_SEVERE:
                severe = True
    hrv = [x["hrv"] for x in well if x.get("hrv")]
    if len(hrv) >= 4 and hrv[-1] < 0.85 * (sum(hrv[:-1]) / len(hrv[:-1])):
        flags.append("HRV more than 15% below its 7-day average")
    sleep = [x["sleep_h"] for x in well if x.get("sleep_h")]
    if sleep and sum(sleep) / len(sleep) < 6.0:
        flags.append("average sleep under 6 h")
    rhr = [x["resting_hr"] for x in well if x.get("resting_hr")]
    if len(rhr) >= 4 and rhr[-1] > sum(rhr[:-1]) / len(rhr[:-1]) + 5:
        flags.append("resting HR 5+ bpm above its 7-day average")
    level = "low" if (severe or len(flags) >= 2) else "moderate" if flags else "good"
    return level, flags


def adherence(prev_plan, acts):
    by_day = {}
    for a in acts:
        d = (a.get("start_date_local") or "")[:10]
        by_day[d] = by_day.get(d, 0) + (a.get("moving_time") or 0) / 60
    start = dt.date.fromisoformat(prev_plan["plan_start"])
    rows = []
    for i, d in enumerate(prev_plan["plan"]):
        date = (start + dt.timedelta(days=i)).isoformat()
        rows.append({"date": date, "planned": d["type"], "planned_min": d["minutes"],
                     "actual_min": round(by_day.get(date, 0))})
    planned = sum(r["planned_min"] for r in rows)
    done = sum(min(r["actual_min"], r["planned_min"]) for r in rows)
    return rows, (round(100 * done / planned) if planned else None)


# ------------------------ km estimate and validation ------------------------
def _mid_sec(p):
    """Midpoint of a pace range like '6:54-7:36/km' in sec/km."""
    lo, hi = p.split("/km")[0].split("-")
    f = lambda x: int(x.split(":")[0]) * 60 + int(x.split(":")[1])
    return (f(lo) + f(hi)) / 2


def est_km(d, paces):
    """Rough distance estimate from minutes and easy/long/race pace (guide only)."""
    if d["type"] in ("rest", "cross"):
        return 0
    key = "race_pace" if d["type"] == "race" else "long" if d["type"] == "long" else "easy"
    return d["minutes"] * 60 / _mid_sec(paces[key])


def validate(plan, days_out, prev_minutes, ready_level, phase_key=None, prev_long=None):
    errs = []
    if [d.get("day") for d in plan] != DAYS:
        return ["Plan must contain exactly 7 entries in order Mon, Tue, Wed, Thu, Fri, Sat, Sun."]
    types = [d["type"] for d in plan]
    race_week = days_out <= 6

    if race_week:
        if types[days_out] != "race":
            errs.append(f"{DAYS[days_out]} must be type 'race'.")
        if types.count("race") != 1:
            errs.append("Exactly one 'race' day is allowed.")
    elif "race" in types:
        errs.append("No 'race' day this week.")

    longs = types.count("long")
    if race_week and longs:
        errs.append("No long run in race week.")
    if not race_week and longs != 1:
        errs.append("Exactly one long run is required.")
    if not race_week and longs == 1:
        long_i = types.index("long")
        lm = plan[long_i]["minutes"]
        cap = LONG_CAP.get(phase_key, 90)
        if lm > cap:
            errs.append(f"{DAYS[long_i]} long run {lm} min exceeds the {phase_key} phase cap of {cap} min.")
        if prev_long and lm > prev_long + LONG_RUN_MAX_INCREASE:
            errs.append(f"{DAYS[long_i]} long run {lm} min is more than {LONG_RUN_MAX_INCREASE} min "
                        f"longer than last week's {prev_long} min long run.")

    q_idx = [i for i, t in enumerate(types) if t in QUALITY]
    if len(q_idx) > 1:
        errs.append("At most 1 full quality session (threshold/interval/race_pace).")
    if ready_level == "low" and q_idx:
        errs.append("Readiness is low: no full quality session this week, strides only.")
    for i in q_idx:
        if plan[i]["work_minutes"] <= 0:
            errs.append(f"{DAYS[i]} quality session needs work_minutes above 0.")
    hard_idx = q_idx + [i for i, t in enumerate(types) if t == "race"]
    for i in hard_idx:
        if i + 1 < 7 and (types[i + 1] in QUALITY or types[i + 1] in ("long", "race")):
            errs.append(f"{DAYS[i]} hard session is followed by another hard day or the long run.")
        if i - 1 >= 0 and types[i - 1] in QUALITY:
            errs.append("Hard sessions on consecutive days.")

    has_speed = bool(q_idx) or "fartlek" in types or any(d["strides"] for d in plan)
    if not has_speed and not race_week:
        errs.append("At least one speed session is required (strides, fartlek, or a quality session).")

    runs = sum(t not in ("rest", "cross") for t in types)
    if runs > 6 or runs < 3:
        errs.append("Plan 3-6 runs this week.")
    total = sum(d["minutes"] for d in plan if d["type"] not in ("rest", "cross"))
    if prev_minutes >= 60:
        if total > prev_minutes * 1.10 + 10:
            errs.append(f"Total {total} min exceeds last week ({prev_minutes} min) by more than 10%.")
        if race_week and total > prev_minutes * 0.75:
            errs.append("Race week total must be at most 75% of last week.")
    return errs


def dosing_notes(plan, paces):
    """Soft Daniels-style dose checks. Reported as notes, they do not trigger a retry."""
    notes = []
    tot_km = sum(est_km(d, paces) for d in plan) or 1
    tot_min = sum(d["minutes"] for d in plan if d["type"] not in ("rest", "cross")) or 1
    for d in plan:
        if d["type"] == "threshold":
            km = d["work_minutes"] * 60 / _mid_sec(paces["threshold"])
            if km > 0.12 * tot_km:
                notes.append(f"{d['day']}: T work about {km:.1f} km is above ~10% of weekly distance.")
        if d["type"] == "interval":
            km = d["work_minutes"] * 60 / _mid_sec(paces["interval"])
            if km > 0.10 * tot_km or km > 10:
                notes.append(f"{d['day']}: I work about {km:.1f} km is above ~8% of weekly distance.")
        if d["type"] == "long" and (d["minutes"] > 150 or d["minutes"] > 0.30 * tot_min):
            notes.append(f"{d['day']}: long run is above ~30% of weekly minutes or 150 min.")
    return notes


# ------------------------- directive retry guidance -------------------------
_RETRY_MAP = [
    ("phase cap of", "Shorten the long run to fit the stated cap. Do not add speed to make up the time."),
    ("longer than last week's", "Reduce the long run so it is close to (not more than 15 min above) last week's long run."),
    ("exceeds last week", "Reduce total weekly minutes: shorten or drop one easy run. Keep the quality session and the long run as they are."),
    ("at most 75% of last week", "Cut every day proportionally so the week's total is at most 75% of last week's."),
    ("Exactly one long run is required", "Add exactly one 'long' day at easy pace; do not add a second one."),
    ("No long run in race week", "Remove the 'long' day this week; race week has no separate long run."),
    ("must be type 'race'", "Set that exact day to type 'race'."),
    ("Exactly one 'race' day is allowed", "Keep only one day with type 'race'."),
    ("No 'race' day this week", "Remove the 'race' day; it is not race week yet."),
    ("work_minutes above 0", "Set work_minutes above 0 on the quality day; it should not be 0 on a threshold/interval/race_pace day."),
    ("followed by another hard day", "Move the quality session so it is not next to the long run or another hard day."),
    ("consecutive days", "Move the quality session so it is not next to the long run or another hard day."),
    ("At most 1 full quality session", "Keep only one threshold/interval/race_pace day; change any other quality day to easy."),
    ("Readiness is low", "Change the quality session to an easy run with strides; do not remove the speed touch entirely."),
    ("At least one speed session is required", "Add strides (set strides: true) on one existing easy day. Do not add a new day."),
    ("Plan 3-6 runs this week", "Keep the number of running days (excluding rest/cross) between 3 and 6."),
    ("exactly 7 entries", "Return exactly 7 entries, one per day, in order Mon, Tue, Wed, Thu, Fri, Sat, Sun."),
]


def retry_advice(errs):
    hints, seen = [], set()
    for e in errs:
        for needle, hint in _RETRY_MAP:
            if needle in e and hint not in seen:
                hints.append(hint)
                seen.add(hint)
    return hints


# --------------------------------- model ---------------------------------
def ask_model(ollama_url, model, prompt, num_ctx=8192, timeout=1800):
    r = requests.post(ollama_url, json={
        "model": model,
        "stream": False,
        "think": False,
        "format": SCHEMA,
        "options": {"num_ctx": num_ctx, "temperature": 0.3},
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": prompt}],
    }, timeout=timeout)
    r.raise_for_status()
    return json.loads(r.json()["message"]["content"])


# ------------------------------ orchestration ------------------------------
def generate_plan(cfg, today=None, on_progress=None):
    """cfg keys: athlete_id, api_key, model, ollama_url, out_dir (Path), max_tries,
    race_date (date), race_km (int), goal_time (str), result_km (int),
    result_time (str), sport_notes (str).

    Returns a dict with everything needed to render, save, and submit the plan,
    or {"post_race": True} once the race date has passed. Raises GenerationError
    for anything that stops generation (Intervals.icu or Ollama failures).
    """
    def progress(msg):
        if on_progress:
            on_progress(msg)

    today = today or dt.date.today()
    base = f"https://intervals.icu/api/v1/athlete/{cfg['athlete_id']}"
    auth = ("API_KEY", cfg["api_key"])

    plan_start = today + dt.timedelta(days=(7 - today.weekday()) % 7)  # next Monday (today if Monday)
    days_out = (cfg["race_date"] - plan_start).days
    phase_text, phase_key = phase_for(days_out)
    if phase_key == "post-race":
        return {"post_race": True}

    vdot = vdot_from_result(cfg["result_km"], _to_sec(cfg["result_time"]))
    paces = build_paces(vdot, cfg["goal_time"], cfg["race_km"])
    predicted = equiv_time(vdot, cfg["race_km"])
    goal_vdot = vdot_from_result(cfg["race_km"], _to_sec(cfg["goal_time"]))

    progress("Fetching Intervals.icu data...")
    try:
        this_acts = intervals_get(base, auth, "activities", today - dt.timedelta(days=6), today)
        prev_acts = intervals_get(base, auth, "activities", today - dt.timedelta(days=13), today - dt.timedelta(days=7))
        wellness = intervals_get(base, auth, "wellness", today - dt.timedelta(days=6), today)
    except requests.RequestException as e:
        raise GenerationError(f"Intervals.icu request failed: {e}") from e

    out_dir = cfg["out_dir"]
    prev_plan, adh_rows, adh_pct, prev_long = None, None, None, None
    prev_file = out_dir / f"plan-{plan_start - dt.timedelta(days=7)}.json"
    if prev_file.exists():
        try:
            prev_plan = json.loads(prev_file.read_text())
            ps = dt.date.fromisoformat(prev_plan["plan_start"])
            prev_week_acts = intervals_get(base, auth, "activities", ps, ps + dt.timedelta(days=6))
            adh_rows, adh_pct = adherence(prev_plan, prev_week_acts)
            prev_long = next((d["minutes"] for d in prev_plan["plan"] if d["type"] == "long"), None)
        except (requests.RequestException, json.JSONDecodeError, KeyError):
            pass  # missing/odd previous plan just means no adherence data this week

    this_w, prev_w = week_stats(this_acts), week_stats(prev_acts)
    sessions = [{k: a.get(k) for k in ("start_date_local", "name", "type", "distance",
                                       "moving_time", "icu_training_load", "average_heartrate")}
                for a in this_acts]
    well = [{"date": x.get("id"), "ctl": x.get("ctl"), "atl": x.get("atl"),
             "resting_hr": x.get("restingHR"), "hrv": x.get("hrv"),
             "sleep_h": round(x["sleepSecs"] / 3600, 1) if x.get("sleepSecs") else None}
            for x in wellness]
    ready_level, ready_flags = readiness(well)

    adh_text = ("No saved plan from last week." if adh_rows is None else
                f"Completion: {adh_pct}% of planned minutes (use this exact number, do not recompute it "
                f"from the day-by-day rows). Day by day: {json.dumps(adh_rows)}")

    prompt = f"""Goal: {cfg['race_km']}K race on {cfg['race_date']} (Sunday), goal time {cfg['goal_time']}.
Fitness: VDOT {vdot:.1f} (from {cfg['result_km']}K in {cfg['result_time']}); predicted {cfg['race_km']}K {_hmmss(predicted)}; goal needs VDOT {goal_vdot:.1f}.
Constraints: {cfg['sport_notes']}
Plan week starts Monday {plan_start}. Days to race from plan start: {days_out}.
Current phase: {phase_text}

Paces (use only these): {json.dumps(paces)}

Readiness: {ready_level}. Flags: {json.dumps(ready_flags)}
Last week's plan vs actual: {adh_text}

Last 7 days totals: {json.dumps(this_w)}
Previous 7 days totals: {json.dumps(prev_w)}
Sessions last 7 days: {json.dumps(sessions)}
Daily wellness (ctl = fitness, atl = fatigue): {json.dumps(well)}

Return JSON: "summary" = at most 5 bullets (volume vs previous week, intensity, fatigue/readiness,
adherence, one thing done well or one concern). If the goal VDOT is well above the current VDOT, say
so honestly in one bullet. "plan" = 7 entries Mon..Sun. Use type "rest" with 0 minutes for rest days.
Set strides true only when strides are added after an easy run."""

    progress(f"Asking {cfg['model']} (this takes a few minutes on CPU)...")
    result, errs = None, []
    for attempt in range(1, cfg.get("max_tries", 3) + 1):
        if errs:
            hints = retry_advice(errs)
            extra = ("\n\nYour previous plan broke these rules:\n- " + "\n- ".join(errs) +
                     "\n\nMake the smallest possible change to fix them, do not redesign the week. "
                     "Specific fixes:\n- " + "\n- ".join(hints))
        else:
            extra = ""
        try:
            result = ask_model(cfg["ollama_url"], cfg["model"], prompt + extra)
        except (requests.RequestException, json.JSONDecodeError, KeyError) as e:
            raise GenerationError(f"Ollama call failed: {e}") from e
        errs = validate(result["plan"], days_out, this_w["minutes"], ready_level, phase_key, prev_long)
        if not errs:
            break
        progress(f"Attempt {attempt}: plan rejected: {'; '.join(errs)}")

    notes = dosing_notes(result["plan"], paces)
    for i, d in enumerate(result["plan"]):
        d["date"] = (plan_start + dt.timedelta(days=i)).isoformat()
        d["est_km"] = round(est_km(d, paces), 1)

    return {
        "post_race": False,
        "plan_start": plan_start,
        "days_out": days_out,
        "phase_text": phase_text,
        "phase_key": phase_key,
        "vdot": round(vdot, 1),
        "predicted": _hmmss(predicted),
        "goal_vdot": round(goal_vdot, 1),
        "paces": paces,
        "readiness": ready_level,
        "readiness_flags": ready_flags,
        "adherence_pct": adh_pct,
        "this_week": this_w,
        "prev_week": prev_w,
        "summary": result["summary"],
        "plan": result["plan"],
        "warnings": errs,
        "dose_notes": notes,
    }


def render_markdown(result):
    ps = result["plan_start"]
    lines = [f"# Training week of {ps} ({result['days_out']} days to race)", "",
             f"Phase: {result['phase_text']}", "",
             f"VDOT {result['vdot']}. Predicted race time: {result['predicted']}. "
             f"Goal needs VDOT {result['goal_vdot']}.",
             f"Readiness: {result['readiness']}" +
             (f" ({'; '.join(result['readiness_flags'])})" if result["readiness_flags"] else ""),
             f"Paces: {json.dumps(result['paces'])}", "", "## Last week summary"]
    lines += [f"- {s}" for s in result["summary"]]
    lines += ["", "## Next week plan"]
    if result["warnings"]:
        lines.append("WARNING: validator still found issues, review carefully: " + "; ".join(result["warnings"]))
    for d in result["plan"]:
        date = dt.date.fromisoformat(d["date"])
        strides = " + strides" if d["strides"] else ""
        work = f" [{d['work_minutes']} min at pace]" if d["work_minutes"] else ""
        km_txt = f" (~{d['est_km']} km)" if d["est_km"] else ""
        lines.append(f"- {date:%a %d %b}: {d['type']} {d['minutes']} min{km_txt}{work}{strides} - {d['details']}")
    tot_min = sum(d["minutes"] for d in result["plan"] if d["type"] not in ("rest", "cross"))
    tot_km = sum(d["est_km"] for d in result["plan"])
    lines += ["", f"Week total: {tot_min} min (~{tot_km:.0f} km est.). "
                  f"Last week: {result['this_week']['minutes']} min, {result['this_week']['km']} km."]
    if result["dose_notes"]:
        lines += ["", "Dose notes (not enforced):"] + [f"- {n}" for n in result["dose_notes"]]
    lines += ["", "Draft only. Review before following, especially for injury risk."]
    return "\n".join(lines)


def save_plan(result, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    ps = result["plan_start"]
    md = render_markdown(result)
    (out_dir / f"plan-{ps}.md").write_text(md)
    (out_dir / f"plan-{ps}.json").write_text(json.dumps(
        {"plan_start": ps.isoformat(), "vdot": result["vdot"], "plan": result["plan"]}, indent=1))
    return md


# ------------------------- push plan to Intervals.icu -------------------------
# Event shape confirmed from Intervals.icu's own forum guide on uploading planned
# workouts (category "WORKOUT", start_date_local, type, name, description,
# moving_time) plus the bulk upsert endpoint with external_id, which lets you
# click Submit again later in the week without creating duplicate events.
# https://forum.intervals.icu/t/uploading-planned-workouts-to-intervals-icu/63624
def build_intervals_events(result, start_hour="06:00"):
    ps = result["plan_start"]
    events = []
    for d in result["plan"]:
        if d["type"] in ("rest", "cross"):
            continue
        label = d["type"].replace("_", " ").title()
        name = f"Race: {d['details']}" if d["type"] == "race" and d["details"] else \
               ("Race" if d["type"] == "race" else label)
        desc_bits = [d["details"]] if d["details"] else []
        if d["work_minutes"]:
            desc_bits.append(f"{d['work_minutes']} min at pace")
        if d["strides"]:
            desc_bits.append("+ strides")
        events.append({
            "category": "WORKOUT",
            "start_date_local": f"{d['date']}T{start_hour}:00",
            "type": "Run",
            "name": name,
            "description": " \u2014 ".join(desc_bits),
            "moving_time": d["minutes"] * 60,
            "external_id": f"weekly-plan-{ps.isoformat()}-{d['date']}",
        })
    return events


def submit_to_intervals(cfg, events):
    if not events:
        return []
    base = f"https://intervals.icu/api/v1/athlete/{cfg['athlete_id']}"
    auth = ("API_KEY", cfg["api_key"])
    r = requests.post(f"{base}/events/bulk", params={"upsert": "true"},
                       json=events, auth=auth, timeout=60)
    r.raise_for_status()
    return r.json()
