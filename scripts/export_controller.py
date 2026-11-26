"""Export the controller tables the Claude dashboard reads, as one JSON file (read-only).

    python -I export_controller.py <repo root> <out.json>
"""
import datetime as dt
import json
import re
import sys

import duckdb

root, out = sys.argv[1], sys.argv[2]
con = duckdb.connect()
con.sql("SET TimeZone='UTC'")
R = lambda t: f"read_parquet('{root}/database/controller/controller_{t}/**/*.parquet', union_by_name=true)"
L = lambda t, k: f"(select * from {R(t)} qualify row_number() over(partition by {k} order by recorded_at desc)=1)"
con.sql(f"create temp table ideas as select * from {L('ideas', 'idea_id')}")
con.sql(f"create temp table res as select * from {R('results')}")
con.sql(f"create temp table jobs as select * from {L('jobs', 'job_id')}")
con.sql(f"create temp table fnd as select * from {L('findings', 'finding_id')}")
NY = "strftime(cast(timezone('America/New_York', cast({} as timestamptz)) - interval 4 hour as date), '%Y-%m-%d')"
GRP = """case when source='person' then 'Person' when source='scout' then 'AI idea scout'
  when source in ('watch_lead','case_lead') then 'AI leads (watch, case)' when source='follow_up' then 'Follow-ups of findings'
  else 'Older lists (imported)' end"""


def q(sql):
    rel = con.sql(sql)
    cols = rel.columns
    rows = []
    for r in rel.fetchall():
        d = {}
        for c, v in zip(cols, r):
            if isinstance(v, (dt.datetime, dt.date)):
                v = str(v)
            elif isinstance(v, float):
                v = round(v, 4)
            d[c] = v
        rows.append(d)
    return rows


def short_title(text):
    """A readable one-line title from an idea's text: drop the researcher's bookkeeping, keep the first sentence."""
    t = re.sub(r"\s+", " ", text or "").replace("`", "").strip()
    t = re.sub(r"^A new idea of the researcher's own[^:.]*(\([^)]*\))?[^:.]*[:.]\s*", "", t, flags=re.I)
    t = re.sub(r"^World map \(thread \d+[^)]*\):\s*", "", t, flags=re.I)
    t = re.sub(r"^(M\d{4}|The thought|Drill of [^:]*):\s*", "", t, flags=re.I)
    for _ in range(3):  # parentheses can nest one or two deep
        t = re.sub(r"\s*\([^()]*\)", "", t)
    sentences = re.split(r"(?<=[a-z0-9%\"'])[.;]\s+(?=[A-Z\"])", t)
    noise = re.compile(r"\bm\d{4}\b|rotation|lab[/\\]|research[/\\]|searched|so far|recorded as|world-map strategies|every (earlier|trigger)|asked|this one is|every one of them|the note", re.I)
    pick = next((s for s in sentences if not noise.search(s) and len(s) > 12), None)
    if pick is None:
        return "Follow-up check of an earlier idea"
    pick = re.sub(r"\b([A-Z]{3,})\b", lambda m: m.group(1).lower() if m.group(1) not in ("US", "FTSE", "VIX", "OPEC", "ETF") else m.group(1), pick)
    pick = pick.strip(" .,:;")
    if len(pick) > 90:
        pick = pick[:90].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return pick[:1].upper() + pick[1:]


ref = con.sql("select max(recorded_at) from jobs").fetchone()[0]
X = {}
X["ideas_by_source"] = q(f"select {GRP} source_group, status, count(*) ideas from ideas group by all order by 1, 3 desc")
X["ideas_people"] = q("""select i.idea_id, i.author, i.title, i.status, i.created_at,
    (select count(*) from jobs j where j.idea_id=i.idea_id) jobs from ideas i where source='person' order by created_at, idea_id""")
X["idea_results"] = q(f"""select r.idea_id, i.title, coalesce(i.text, i.title) idea_text, {GRP} source_group, i.author, i.status,
    (select count(*) from jobs j where j.idea_id=r.idea_id) jobs,
    count(*) judged, count(*) filter (where verdict='pass') passed, count(*) filter (where verdict='pass') / count(*) pass_rate,
    max(finished_at) last_result
  from res r left join ideas i using (idea_id)
  where not r.is_control and verdict in ('pass','fail') and r.idea_id is not null
  group by all having count(*) >= 10 order by passed desc, pass_rate desc, last_result desc limit 60""")
for r in X["idea_results"]:
    r["short_title"] = short_title(r.pop("idea_text") or r["title"])
X["noise"] = q(f"""select {NY.format('finished_at')} "day", stage,
    case when not is_control then 'real' when control_kind='planted' then 'planted' else 'luck' end kind,
    count(*) judged, count(*) filter (where verdict='pass') passed
  from res where verdict in ('pass','fail') and finished_at is not null group by all order by 1, 2, 3""")
X["funnel"] = q("""select * from (values
  (1, 'Ideas written', (select count(*) from ideas)),
  (2, 'Sent to a test', (select count(*) from ideas where status not in ('new','question','duplicate'))),
  (3, 'Have a test result', (select count(distinct idea_id) from res where not is_control and verdict in ('pass','fail'))),
  (4, 'Passed at least once', (select count(distinct idea_id) from res where not is_control and verdict='pass')),
  (5, 'Became a finding', (select count(*) from fnd where finding_id like 'F%')),
  (6, 'Confirmed finding', (select count(*) from fnd where status='confirmed')),
  (7, 'Followed live (rules)', (select count(distinct log) from """ + R("predictions") + """))
) t(step_no, step, n)""")
X["jobs_origin"] = q("select origin, state, count(*) jobs from jobs group by all order by 3 desc")
X["machine_day"] = q(f"""select coalesce(claimed_by, 'not claimed') machine, {NY.format('finished_at')} "day", count(*) jobs,
    case when max(claimed_by) is null then null else round(sum(seconds) / 3600, 1) end busy_hours
  from jobs where finished_at is not null and finished_at >= timestamp '2026-11-12' group by 1, 2 order by 2, 1""")
X["beats"] = q(f"""select strftime(night, '%Y-%m-%d') night, "at" at_utc, gpu_busy_share, running, jobs_done, claimable_gpu, claimable_cpu, machine
  from {L('window_beats', 'beat_id')} order by "at" """)
X["windows"] = q(f"""select window_id, strftime(night, '%Y-%m-%d') night, started_at started_utc, ended_at ended_utc, end_kind, pieces_done,
    round(epoch(ended_at - started_at) / 3600, 1) window_hours
  from {L('windows', 'window_id')} where ended_at is not null order by started_at""")
X["stuck"] = q(f"""select job_id, origin, kind, claimed_by, started_at started_utc,
    round(epoch(timestamp '{ref}' - started_at) / 3600, 1) hours_open, idea_id
  from jobs where state='running' and started_at is not null order by started_at, job_id""")
X["findings_detail"] = q(f"""select f.finding_id, f.status, f.title, f.statement, f.use, t.verdict tradable, t.reason tradable_reason, f.updated_at
  from fnd f left join (select * from {R('tradability_checks')} qualify row_number() over(partition by finding_id order by checked_at desc, recorded_at desc)=1) t
    using (finding_id)
  where f.finding_id like 'F%' order by try_cast(substr(f.finding_id, 2) as int), f.finding_id""")
X["scoreboard"] = q(f"""select strftime("day", '%Y-%m-%d') "day", metric, slice, period, n, value, baseline, p
  from {R('scoreboard')} where "day"=(select max("day") from {R('scoreboard')})
  qualify row_number() over(partition by "day", metric, slice, period order by recorded_at desc)=1 order by metric, slice, period""")
X["meta"] = q(f"""select (select max(finished_at) from res) newest_result_utc,
    (select max("at") from {R('window_beats')}) newest_beat_utc,
    (select max(ended_at) from {R('windows')}) newest_window_end_utc,
    strftime(now(), '%Y-%m-%d %H:%M:%S') || '+00' exported_utc""")
json.dump(X, open(out, "w"), separators=(",", ":"), default=str)
print({k: len(v) for k, v in X.items()}, X["meta"])
