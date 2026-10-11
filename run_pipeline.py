"""Daily job: scrape website -> read CRM -> match -> queue NEW proposals for review.

Safe to re-run: proposals already in the queue (pending, approved, rejected or
failed) are never inserted again, and nothing is written to the CRM here.
"""
import json
import os
from collections import Counter
from datetime import datetime, timezone

import crm
import db
import matcher
import scraper


def main():
    run_id = datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%SZ")
    os.makedirs("data", exist_ok=True)

    locations = scraper.scrape()
    accounts = crm.list_accounts()
    json.dump(locations, open("data/website_locations.json", "w"), indent=1)

    proposals = matcher.build_proposals(locations, accounts)
    conn = db.connect()
    stats = Counter()
    for p in proposals:
        if p["type"] == "confirmed":
            stats["confirmed (no change)"] += 1
            continue
        if db.has_open_proposal_for(conn, p["subject"]) and not conn.execute(
                "SELECT 1 FROM proposals WHERE dedupe_key=?", (db.dedupe_key(p),)).fetchone():
            # Something about this record changed while an older proposal is still
            # waiting. Don't stack a second change on it; it will be re-evaluated
            # after the pending one is decided.
            stats["deferred (record already has a pending proposal)"] += 1
            continue
        if db.insert_if_new(conn, run_id, p):
            stats[f"new: {p['type']}"] += 1
        else:
            stats["skipped (already decided or queued)"] += 1
    summary = {"locations_on_website": len(locations), "crm_accounts": len(accounts), **stats}
    conn.execute("INSERT INTO runs (run_id, started_at, summary) VALUES (?,?,?)", (run_id, db.now(), json.dumps(summary)))
    conn.commit()

    print(f"{run_id}")
    for k, v in summary.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
