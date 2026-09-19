#!/usr/bin/env python3
"""The next scheduled exercise is learned from the controller, not the cloud.

Regression cover for the case that motivated it: a MANUAL run must not move
the prediction, because the controller does not stamp last_exercise for one.
"""
import sys, pathlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

@dataclass
class _Runtime:
    exercise_seen: list = field(default_factory=list)

class _Coord:
    """Mirror of EnergyTrakCoordinator._derive_next_exercise."""
    def __init__(self): self.sites = {}
    def derive(self, site_id, payload):
        raw = payload.get("last_exercise_at")
        if not raw: return
        rt = self.sites.setdefault(site_id, _Runtime())
        stamp = str(raw)
        if stamp not in rt.exercise_seen:
            rt.exercise_seen = (rt.exercise_seen + [stamp])[-4:]
        try: last = datetime.fromisoformat(stamp)
        except ValueError: return
        interval = timedelta(days=7)
        if len(rt.exercise_seen) >= 2:
            try:
                gap = last - datetime.fromisoformat(rt.exercise_seen[-2])
                if timedelta(hours=20) <= gap <= timedelta(days=36): interval = gap
            except ValueError: pass
        payload["next_exercise_due"] = (last + interval).isoformat()
        payload["exercise_interval_days"] = round(interval.total_seconds()/86400, 2)

fails = []
def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got  {got}\n        want {want}"); fails.append(name)

SAT = "2026-09-19T11:23:50+00:00"     # the real observed scheduled exercise

c = _Coord(); p = {"last_exercise_at": SAT}; c.derive("s", p)
check("one observation defaults to weekly", p["exercise_interval_days"], 7.0)
check("next is a week after the scheduled run",
      p["next_exercise_due"], "2026-09-26T11:23:50+00:00")

# A MANUAL run happens. The controller does NOT restamp last_exercise, so the
# payload is unchanged -- and the prediction must not move. This is exactly
# where the cloud goes wrong: it slides nextExerciseDue to the manual run.
p2 = {"last_exercise_at": SAT}; c.derive("s", p2)
check("a manual run does not move the prediction",
      p2["next_exercise_due"], "2026-09-26T11:23:50+00:00")
check("and does not invent a second observation",
      len(c.sites["s"].exercise_seen), 1)

c2 = _Coord()
for stamp in ("2026-09-12T11:20:10+00:00", SAT):
    q = {"last_exercise_at": stamp}; c2.derive("s", q)
check("two weekly runs learn a 7 day interval", q["exercise_interval_days"], 7.0)

c3 = _Coord()
for stamp in ("2026-08-19T11:20:10+00:00", SAT):
    r = {"last_exercise_at": stamp}; c3.derive("s", r)
check("two monthly runs learn ~31 days", r["exercise_interval_days"], 31.0)

c4 = _Coord()
for stamp in (SAT, "2026-09-19T16:51:55+00:00"):
    t = {"last_exercise_at": stamp}; c4.derive("s", t)
check("an implausible 5h gap is rejected", t["exercise_interval_days"], 7.0)

c5 = _Coord(); u = {}; c5.derive("s", u)
check("no last_exercise means no prediction", u.get("next_exercise_due"), None)

print("\n  FAILED" if fails else "\n  all passed")
sys.exit(1 if fails else 0)
