"""Pre-race push-notification script (runs from GitHub Actions cron).

For each race scheduled today, fires a single ntfy.sh push notification when
the race is ~10 minutes from its scheduled post time. Picks come from the
model's race-day report (ET top-3 + SARR top-3, mutual highlighted) so the
phone alert tells the user "race X is up next, here's our angle".

State (which races have been notified) is held in
``cache/notify_state_YYYYMMDD.json`` so re-runs of the cron do not duplicate.

Designed to be invoked every 5 minutes during HK race-day windows. Window
is ``[POST-12 min, POST-8 min]``; running every 5 min guarantees one hit
per race regardless of cron jitter. Uses a per-race lock in the state file
to suppress duplicates if two cron runs overlap.

Required env vars:
    NTFY_TOPIC          private ntfy.sh topic UUID (also Streamlit secret)

Optional env vars:
    NOTIFY_LEAD_MIN     centre of fire-window in minutes (default 10)
    NOTIFY_DRY_RUN=1    print plan, do not push or write state
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
CACHE = BASE / "cache"
REPORTS = BASE / "reports"

HK_TZ = timezone(timedelta(hours=8))  # Asia/Hong_Kong (fixed offset, no DST)
LEAD_MIN = int(os.environ.get("NOTIFY_LEAD_MIN", "10"))
WINDOW_MIN = 2  # +/- minutes either side of LEAD_MIN
DRY_RUN = os.environ.get("NOTIFY_DRY_RUN", "") == "1"


def _ntfy_topic() -> str:
    return os.environ.get("NTFY_TOPIC", "").strip()


def _push(title: str, body: str, tags: str = "racing,horse_racing",
          priority: int = 4) -> bool:
    topic = _ntfy_topic()
    if not topic:
        print(f"[notify_races] NTFY_TOPIC not set; would have sent: {title}")
        return False
    if DRY_RUN:
        print(f"[notify_races] DRY_RUN: {title}\n{body}\n")
        return True
    try:
        import requests
        resp = requests.post(
            f"https://ntfy.sh/{topic}",
            data=body.encode("utf-8"),
            headers={
                "Title": title[:100],
                "Tags": tags,
                "Priority": str(priority),
            },
            timeout=10,
        )
        return resp.status_code == 200
    except Exception as e:
        print(f"[notify_races] push failed: {e}", file=sys.stderr)
        return False


def _load_racecard(today: date) -> dict | None:
    fp = CACHE / f"racecard_{today.isoformat()}.json"
    if not fp.exists():
        return None
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_report(today: date, model_tag: str) -> dict | None:
    """Load race_day_report (ET or SARR variant). model_tag = 'v4.4' / 'SARR' etc."""
    dc = today.strftime("%Y%m%d")
    # Try the explicit tag first, then fall back to any matching report.
    if model_tag == "ET":
        for fp in sorted(REPORTS.glob(f"race_day_report_{dc}_v*.json"),
                         reverse=True):
            try:
                return json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                continue
        return None
    fp = REPORTS / f"race_day_report_{dc}_{model_tag}.json"
    if fp.exists():
        try:
            return json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _state_path(today: date) -> Path:
    return CACHE / f"notify_state_{today.strftime('%Y%m%d')}.json"


def _load_state(today: date) -> dict:
    fp = _state_path(today)
    if not fp.exists():
        return {"sent": {}}
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return {"sent": {}}


def _save_state(today: date, state: dict) -> None:
    if DRY_RUN:
        return
    fp = _state_path(today)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _parse_race_time(s: str, today: date) -> datetime | None:
    """Parse 'HH:MM' Hong Kong time into a tz-aware datetime."""
    if not s or ":" not in s:
        return None
    try:
        hh, mm = s.split(":", 1)
        return datetime(today.year, today.month, today.day,
                        int(hh), int(mm), tzinfo=HK_TZ)
    except Exception:
        return None


def _top_n(picks: list[dict], n: int = 3) -> list[tuple[int, str]]:
    out = []
    for p in (picks or [])[:n]:
        try:
            out.append((int(p["horse_no"]), str(p.get("horse_name", ""))))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _build_body(rn: int, et_picks: list[dict],
                sarr_picks: list[dict] | None) -> str:
    et4 = _top_n(et_picks, 4)
    s4 = _top_n(sarr_picks or [], 4)
    et4_nos = {no for no, _ in et4}
    s4_nos = {no for no, _ in s4}
    mutual = et4_nos & s4_nos

    lines = []
    if et4:
        et_str = " · ".join(
            f"{'★' if no in mutual else ''}#{no} {nm[:14]}"
            for no, nm in et4[:3]
        )
        lines.append(f"ET: {et_str}")
    if s4:
        s_str = " · ".join(
            f"{'★' if no in mutual else ''}#{no} {nm[:14]}"
            for no, nm in s4[:3]
        )
        lines.append(f"SARR: {s_str}")
    if mutual:
        lines.append(f"★ mutual: " + ", ".join(
            f"#{no}" for no in sorted(mutual)
        ))
    return "\n".join(lines) if lines else "(no model picks loaded)"


def run() -> int:
    today = datetime.now(tz=HK_TZ).date()
    now = datetime.now(tz=HK_TZ)
    print(f"[notify_races] tick at {now.isoformat()} (HKT) · "
          f"lead={LEAD_MIN}min ±{WINDOW_MIN}min · dry={DRY_RUN}")

    rc = _load_racecard(today)
    if not rc:
        print(f"[notify_races] no racecard for {today}; nothing to do.")
        return 0
    et_report = _load_report(today, "ET")
    sarr_report = _load_report(today, "SARR")
    et_by_rn = {int(r.get("race_number", 0) or 0): r
                for r in (et_report or {}).get("races", [])}
    sarr_by_rn = {int(r.get("race_number", 0) or 0): r
                  for r in (sarr_report or {}).get("races", [])}

    state = _load_state(today)
    sent = state.setdefault("sent", {})

    fired = 0
    for race in rc.get("races", []):
        meta = race.get("meta") or {}
        rn = meta.get("race_number")
        rt_str = meta.get("race_time")
        if not rn or not rt_str:
            continue
        post = _parse_race_time(rt_str, today)
        if not post:
            continue
        mins_to_post = (post - now).total_seconds() / 60.0
        key = f"R{rn}"
        if sent.get(key):
            continue
        # Fire window: [LEAD - WINDOW, LEAD + WINDOW]
        if not (LEAD_MIN - WINDOW_MIN <= mins_to_post <= LEAD_MIN + WINDOW_MIN):
            continue

        et_picks = (et_by_rn.get(int(rn)) or {}).get("picks") or []
        sarr_picks = (sarr_by_rn.get(int(rn)) or {}).get("picks") or []
        dist = meta.get("distance")
        cls = meta.get("race_class")
        title_bits = [f"⏱ R{rn}", f"~{int(round(mins_to_post))} min"]
        if dist:
            title_bits.append(f"{dist}m")
        if cls is not None:
            title_bits.append(f"Cl{cls}")
        title = " · ".join(title_bits)

        body = _build_body(int(rn), et_picks, sarr_picks)
        ok = _push(title, body,
                   tags="hourglass_flowing_sand,horse_racing",
                   priority=4)
        if ok:
            sent[key] = {
                "fired_at": now.isoformat(),
                "post_time": post.isoformat(),
                "mins_to_post": round(mins_to_post, 2),
            }
            fired += 1
            print(f"[notify_races] fired R{rn} ({mins_to_post:+.1f} min to post)")

    if fired:
        _save_state(today, state)
    print(f"[notify_races] done · fired={fired}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
