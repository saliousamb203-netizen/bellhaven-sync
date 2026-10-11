"""Local review app. Reviewer sees each proposal with evidence and approves or rejects.

Only an approval writes to the CRM. Run:  CRM_TOKEN=... python3 review_app.py
"""
import json

from flask import Flask, redirect, render_template_string, request, url_for

import crm
import db
from config import API_TOKEN, SITE_BASE

app = Flask(__name__)

TYPE_LABELS = {
    "chow": "Change of ownership (SOP)",
    "reparent": "Re-parent to Bellhaven",
    "duplicate": "Duplicate",
    "update_fields": "Field fix",
    "create_new": "New location",
    "not_on_website": "No longer on website",
    "needs_review": "Needs review",
}
TYPE_ORDER = list(TYPE_LABELS)


def crm_link(account_id):
    return f"{SITE_BASE}/crm/{API_TOKEN}/accounts/{account_id}"


# ---------- applying an approved proposal ----------

def apply_actions(actions):
    """Run the proposal's API writes in order. Returns a log of what happened."""
    saved, log = {}, []
    for a in actions:
        if a["op"] == "create":
            created = crm.create_account(a["fields"])
            new_id = created.get("account_id") or (created.get("data") or {}).get("account_id")
            if a.get("save_as"):
                saved["$" + a["save_as"]] = new_id
            log.append(f"created {new_id} ({a['fields']['name']})")
        elif a["op"] == "patch":
            fields = {k: saved.get(v, v) if isinstance(v, str) else v for k, v in a["fields"].items()}
            if "parent_id" in fields:
                # Re-check the CHOW SOP against LIVE data right before writing, in case
                # billing changed since the proposal was generated.
                live = crm.get_account(a["account_id"])
                live = live.get("data", live)
                if (live.get("lifetime_revenue") or 0) > 0 and (live.get("outstanding_ar") or 0) > 0:
                    raise crm.CRMError("Blocked by CHOW SOP: account now has revenue AND outstanding AR; "
                                       "re-run the pipeline so it proposes a CHOW instead of a re-parent.")
            crm.update_account(a["account_id"], fields)
            log.append(f"updated {a['account_id']}: {', '.join(fields)}")
    return log


# ---------- routes ----------

@app.route("/")
def index():
    show = request.args.get("show", "pending")
    conn = db.connect()
    rows = conn.execute(
        "SELECT * FROM proposals WHERE (? = 'all' OR status = ?) ORDER BY id", (show, show)
    ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["actions"] = json.loads(r["actions"])
        d["evidence"] = json.loads(r["evidence"])
        items.append(d)
    items.sort(key=lambda d: (TYPE_ORDER.index(d["type"]) if d["type"] in TYPE_ORDER else 99, d["id"]))
    counts = dict(conn.execute("SELECT status, COUNT(*) FROM proposals GROUP BY status").fetchall())
    last_run = conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    return render_template_string(PAGE, items=items, show=show, counts=counts, labels=TYPE_LABELS,
                                  crm_link=crm_link, last_run=last_run, json=json)


@app.post("/decide/<int:pid>")
def decide(pid):
    decision = request.form["decision"]
    note = request.form.get("reviewer_note", "")
    conn = db.connect()
    row = conn.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
    if row["status"] not in ("pending", "failed"):
        return redirect(url_for("index"))
    if decision == "reject":
        conn.execute("UPDATE proposals SET status='rejected', reviewer_note=?, decided_at=? WHERE id=?",
                     (note, db.now(), pid))
    else:
        try:
            log = apply_actions(json.loads(row["actions"]))
            conn.execute("UPDATE proposals SET status='approved', reviewer_note=?, result=?, decided_at=? WHERE id=?",
                         (note, "\n".join(log), db.now(), pid))
        except Exception as e:  # keep it visible and retryable
            conn.execute("UPDATE proposals SET status='failed', reviewer_note=?, result=?, decided_at=? WHERE id=?",
                         (note, str(e), db.now(), pid))
    conn.commit()
    return redirect(url_for("index", show=request.args.get("show", "pending")) + f"#p{pid}")


PAGE = """
<!doctype html><html><head><meta charset="utf-8"><title>Bellhaven CRM review</title>
<style>
 body{font-family:-apple-system,Segoe UI,Arial,sans-serif;margin:0;background:#f5f6f8;color:#222}
 header{background:#1f3b57;color:#fff;padding:14px 24px} header a{color:#cfe3ff;margin-right:14px}
 main{max-width:1100px;margin:20px auto;padding:0 16px}
 .card{background:#fff;border:1px solid #dde1e6;border-radius:8px;padding:16px 18px;margin-bottom:16px}
 .tag{display:inline-block;font-size:12px;padding:2px 8px;border-radius:10px;background:#e8eef6;margin-right:6px}
 .chow{background:#fde8c8} .duplicate{background:#f3dede} .create_new{background:#dff3e3} .reparent{background:#e3e0f7}
 .not_on_website{background:#f7f0c8}
 table{border-collapse:collapse;width:100%;font-size:13px;margin-top:8px} td,th{border:1px solid #e3e6ea;padding:4px 8px;text-align:left;vertical-align:top}
 th{background:#f0f2f5;width:140px} .diff{background:#fff3c4;font-weight:600}
 pre{background:#f6f8fa;padding:8px;font-size:12px;white-space:pre-wrap;border-radius:4px}
 .btns button{padding:7px 16px;border:0;border-radius:5px;cursor:pointer;font-weight:600;margin-right:8px}
 .ok{background:#2e7d32;color:#fff} .no{background:#c62828;color:#fff}
 .status{font-weight:700} .why{color:#555;font-size:13px;margin:6px 0}
</style></head><body>
<header><b>Bellhaven ownership sync - review queue</b> &nbsp;
 <a href="?show=pending">Pending ({{counts.get('pending',0)}})</a>
 <a href="?show=approved">Approved ({{counts.get('approved',0)}})</a>
 <a href="?show=rejected">Rejected ({{counts.get('rejected',0)}})</a>
 <a href="?show=failed">Failed ({{counts.get('failed',0)}})</a>
 <a href="?show=all">All</a>
 {% if last_run %}<span style="float:right;font-size:12px">Last run {{last_run['started_at']}}</span>{% endif %}
</header><main>
{% if not items %}<p>Nothing here.</p>{% endif %}
{% for p in items %}
<div class="card" id="p{{p.id}}">
 <span class="tag {{p.type}}">{{labels.get(p.type,p.type)}}</span><span class="tag">confidence: {{p.confidence}}</span>
 <span class="status">{{p.status}}</span>
 <h3 style="margin:8px 0">{{p.title}}</h3>
 {% set ev = p.evidence %}
 {% if ev.reasoning %}<div class="why"><b>Why:</b> {{ev.reasoning}}</div>{% endif %}
 {% if ev.match_reasons %}<div class="why"><b>Match:</b> score {{ev.match_score}} - {{ev.match_reasons|join(', ')}}</div>{% endif %}
 <table>
  <tr><th></th><th>Website</th><th>CRM record</th>{% if ev.duplicate or ev.new_operator_account %}<th>{{'Duplicate' if ev.duplicate else 'New operator account'}}</th>{% endif %}</tr>
  {% set w = ev.website or {} %}{% set c = ev.crm or {} %}{% set o = ev.duplicate or ev.new_operator_account or {} %}
  {% for label, wk, ck in [('Name','name','name'),('Street','street','billing_street'),('City','city','billing_city'),('State','state','billing_state'),('Zip','zip','billing_zip'),('Care','care_offerings','care_type'),('Phone','phone','phone'),('Parent',None,'parent_name'),('Status',None,'status'),('Revenue / AR',None,'lifetime_revenue')] %}
   {% set wv = (w.get(wk)|join(', ') if wk=='care_offerings' and w.get(wk) else w.get(wk,'')) if wk else '' %}
   {% set cv = (c.get(ck)|string + ' / ' + c.get('outstanding_ar')|string) if ck=='lifetime_revenue' and c else c.get(ck,'') %}
   {% set ov = (o.get(ck)|string + ' / ' + o.get('outstanding_ar')|string) if ck=='lifetime_revenue' and o else o.get(ck,'') %}
   <tr><th>{{label}}</th><td>{{wv}}</td>
     <td class="{{'diff' if p.type in ('update_fields','reparent') and ck in p.actions[0].get('fields',{}) else ''}}">{{cv}}
       {% if c.account_id and loop.first %}<br><a href="{{crm_link(c.account_id)}}" target="_blank">{{c.account_id}}</a>{% endif %}</td>
     {% if o %}<td>{{ov}}{% if o.account_id and loop.first %}<br><a href="{{crm_link(o.account_id)}}" target="_blank">{{o.account_id}}</a>{% endif %}</td>{% endif %}</tr>
  {% endfor %}
 </table>
 {% if w.url %}<div class="why">Source: <a href="{{w.url}}" target="_blank">{{w.url}}</a>{% if w.source=='homepage_only' %} &nbsp;<b>(announced on homepage; not yet in directory)</b>{% endif %}</div>{% endif %}
 {% if ev.closest_candidates %}<div class="why"><b>Closest CRM candidates (rejected):</b>
   {% for cc in ev.closest_candidates %}<br>- {{cc.account.name}} | {{cc.account.billing_street}}, {{cc.account.billing_city}} {{cc.account.billing_state}} | {{cc.account.parent_name}} | score {{cc.score}}{% endfor %}</div>{% endif %}
 <div class="why"><b>Will write to CRM:</b></div><pre>{{json.dumps(p.actions, indent=1)}}</pre>
 {% if p.result %}<pre>{{p.result}}</pre>{% endif %}
 {% if p.status in ('pending','failed') %}
 <form method="post" action="/decide/{{p.id}}?show={{show}}" class="btns">
  <input name="reviewer_note" placeholder="optional reviewer note" style="width:300px;padding:6px">
  <button class="ok" name="decision" value="approve">Approve &amp; write</button>
  <button class="no" name="decision" value="reject">Reject</button>
 </form>{% elif p.reviewer_note %}<div class="why">Reviewer note: {{p.reviewer_note}}</div>{% endif %}
</div>
{% endfor %}
</main></body></html>
"""

if __name__ == "__main__":
    app.run(port=5050, debug=False)
