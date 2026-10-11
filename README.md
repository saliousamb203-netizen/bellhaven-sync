# Bellhaven ownership sync

Keeps the CRM's facility → parent-company links in sync with what Bellhaven Senior Living publishes on its website. Each run scrapes the site, compares it to the CRM, and queues proposed fixes. A human approves or rejects each one in a small review app. **Nothing is written to the CRM without approval.**

```
scraper.py ──► website locations ─┐
                                  ├─► matcher.py ──► proposals ──► SQLite queue ──► review_app.py ──(approve)──► CRM API
crm.py ──────► CRM accounts ──────┘                                (dedupe keys)      (approve/reject)
```

## Run it

```bash
pip install -r requirements.txt
export CRM_TOKEN=bh_...              # never committed
python3 run_pipeline.py             # scrape + match + queue (no CRM writes)
python3 review_app.py               # http://localhost:5050 → approve / reject
```

Schedule: `.github/workflows/daily-sync.yml` (daily at 10:00 UTC, review DB persisted as an artifact) or `crontab.txt` (cron with `flock` so runs can't overlap).

| File | Role |
|---|---|
| `scraper.py` | Walks the paginated `/communities` directory **and** the homepage (Findlay was announced on the homepage but missing from the directory), then reads every detail page: name, street, city, state, zip, care offerings, phone. |
| `matcher.py` | Matching and classification; builds each proposal with its evidence and the exact API writes. |
| `db.py` | Proposal queue and dedupe keys, so re-runs are safe. |
| `run_pipeline.py` | The daily job. |
| `review_app.py` | Review UI; executes approved actions. |

## Matching approach

1. **Normalize.** Street abbreviations and directionals (`Avenue→ave`, `West→w`, `Northwest→nw`, `Pk→pike`), punctuation, and name variants (`&→and`, `Centre→Center`, `Healthcare→Health Care`).
2. **Strong match = same normalized street + city + state.** This is the primary key, because names are exactly what changes during acquisitions and rebrands.
3. **Fallback** when the CRM street is unusable (for example the PO box on Ashtabula): same city, state **and** zip, **plus** either the same phone or a near-identical name (≥ 0.9). Both signals are required, so a different facility in the same zip can't be pulled in by name alone.
4. **A name alone is never enough.** "Amberly Manor" exists in the CRM, but in Colorado Springs under Juniper Point. That's a different facility, so Hudson, OH gets a new account. The rejected near-misses are shown as evidence.
5. **Several CRM accounts at one address** means the same facility was entered more than once by different operators. One survivor is chosen by: already under Bellhaven › has revenue history › has a parent › Active › match score. The rest become duplicates.
6. Accounts already marked `duplicate_of_account` or `chow_current_account` are treated as resolved history and excluded. That is why a second run doesn't re-propose them.

## How each outcome is handled

| Situation | Example | What gets written on approval |
|---|---|---|
| Confident match, nothing differs | Bellhaven of Maplewood | Nothing; counted as confirmed. |
| Right parent, stale fields | Riverbend Manor Care Center → **Bellhaven of Chagrin Falls**; Portsmouth zip `45626→45662`; Ashtabula PO box → street | `PATCH` only the changed fields (name, street, zip, care type, phone, status). Equivalent formatting like "West Michigan Avenue" vs "W Michigan Ave" is **not** churned. |
| Wrong parent, no revenue history or no AR | Lima (Harborview), Zanesville (Cedar Trail), Kettering, Findlay (no parent) | `PATCH parent_id` → Bellhaven, plus any field fixes. |
| Wrong parent, **revenue AND AR > 0** (SOP) | Tiffin ($84k / $12.4k AR), Marietta ($51k / $3.8k AR) | Old account left **exactly as is**. `POST` a new account under Bellhaven with website data, then `PATCH chow_current_account` on the old account → new id. |
| Duplicate at the same address | Harborview/Cedar Trail copies in Port Clinton, Erie, Monroe, Kettering; second Owosso | `duplicate_of_account` → survivor, `status=Inactive`, explanatory `note`. A "duplicate" with revenue history is never auto-inactivated; it goes to `Needs Review` instead. |
| On website, not in CRM | Batavia, Carlisle, Amberly Manor, Union Square | `POST` new account under Bellhaven. Union Square: the only nearby record is "Union Square Senior Living" at a **different street** (240 Market St) under Juniper Point, so it was treated as a different facility. |
| Under Bellhaven, not on website, new operator found | Sandusky → Millstone Care of Sandusky already exists | Has revenue + AR, so per SOP the old account is preserved and gets `chow_current_account` → the **existing** Millstone account (creating a third record would add a duplicate). |
| Under Bellhaven, not on website, new operator unknown | Alliance, Coldwater | `status=Needs Review` + note. We know it left the website but not who owns it now, so we flag it rather than guess a new parent. |

## Safety

- **No writes without approval.** The pipeline only reads; `review_app.py` writes, and only on Approve.
- **Idempotent re-runs.** Every proposal gets a dedupe key (`type | record | hash of intended field changes`, ignoring the dated note). Pending, approved, rejected or failed proposals are never queued again. Once a change lands, the diff disappears from the next run. A record that already has a pending proposal doesn't get a second one stacked on it.
- **SOP re-checked at write time.** Before any re-parent, the app re-reads the account live; if it now has revenue AND AR, the write is blocked.
- **Failures are visible and retryable** in the app's Failed tab.
- **Token from the environment only**; never committed.

## What I'd build next

- **Notify on new proposals** (Slack or email digest) so the queue doesn't sit unreviewed.
- **Watch for more ownership signals** beyond the operator's own site: state licensing / CMS ownership data, and press releases for acquisitions.
- **Make multi-step approvals resumable.** If the CHOW "create" succeeds but the "link" fails, a retry should reuse the created account instead of creating another.
- **Support multi-value care types.** The CRM `care_type` field holds one value, so the extra website offerings (for example Erie's Memory Support) are dropped.
- **Generalize to every parent company** with a config of operator websites, and run the same pipeline across the book.
