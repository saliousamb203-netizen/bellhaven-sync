"""Match website locations to CRM accounts and turn the differences into proposals.

Every proposal carries:
  type      what kind of change it is
  actions   the exact API writes that run if a reviewer approves
  evidence  why we think so (website vs CRM side by side, plus reasoning)
Nothing here writes to the CRM. Writes only happen in review_app.py on approval.
"""
import difflib
import re
from datetime import date

BELLHAVEN_PARENT_NAME = "Bellhaven Senior Living (Parent Account)"

# Website care offerings -> CRM care_type picklist values
CARE_MAP = {
    "Assisted Living": "Assisted Living",
    "Memory Support": "Memory Care",
    "Memory Care": "Memory Care",
    "Short-Term Rehabilitation & Nursing": "Skilled Nursing",
    "Skilled Nursing": "Skilled Nursing",
    "Independent Living": "Independent Living",
}

STREET_ABBR = {
    "avenue": "ave", "street": "st", "boulevard": "blvd", "road": "rd", "drive": "dr",
    "lane": "ln", "court": "ct", "parkway": "pkwy", "place": "pl", "highway": "hwy",
    "pike": "pike", "pk": "pike", "north": "n", "south": "s", "east": "e", "west": "w",
    "northwest": "nw", "northeast": "ne", "southwest": "sw", "southeast": "se",
}


# ---------- normalization ----------

def norm_street(s):
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    return " ".join(STREET_ABBR.get(w, w) for w in s.split())


def is_po_box(s):
    return bool(re.match(r"\s*p\.?\s*o\.?\s*box", (s or "").lower()))


def norm_name(s):
    s = (s or "").lower().replace("&", " and ")
    s = s.replace("centre", "center").replace("healthcare", "health care").replace("rehab ", "rehabilitation ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(s.split())


def name_sim(a, b):
    return difflib.SequenceMatcher(None, norm_name(a), norm_name(b)).ratio()


def same_place(loc, acct):
    return (
        norm_street(loc["street"]) == norm_street(acct["billing_street"])
        and loc["city"].lower() == (acct["billing_city"] or "").lower()
        and loc["state"] == acct["billing_state"]
    )


def has_chow_history(acct):
    """CHOW rule: revenue history AND outstanding AR > 0 means billing needs the old account kept."""
    return (acct.get("lifetime_revenue") or 0) > 0 and (acct.get("outstanding_ar") or 0) > 0


def account_snapshot(a):
    keys = ["account_id", "name", "parent_name", "billing_street", "billing_city", "billing_state",
            "billing_zip", "care_type", "status", "phone", "lifetime_revenue", "outstanding_ar"]
    return {k: a.get(k) for k in keys}


def site_snapshot(loc):
    return {k: loc[k] for k in ["name", "street", "city", "state", "zip", "care_offerings", "phone", "url", "source"]}


# ---------- candidate scoring ----------

def score(loc, acct):
    s, why = 0, []
    if same_place(loc, acct):
        s += 50; why.append("street + city + state match")
    elif loc["city"].lower() == (acct["billing_city"] or "").lower() and loc["state"] == acct["billing_state"]:
        s += 10; why.append("same city/state")
    if acct["billing_zip"] == loc["zip"]:
        s += 15; why.append("zip match")
    if loc["phone"] and acct.get("phone") == loc["phone"]:
        s += 15; why.append("phone match")
    ns = name_sim(loc["name"], acct["name"])
    s += round(20 * ns); why.append(f"name similarity {ns:.2f}")
    return s, why


def pick_primary(group, bellhaven_id):
    """Choose which account in a same-address group survives.

    Preference: already under Bellhaven > has revenue history > has some parent
    (vs orphan) > Active > best name similarity.
    """
    def key(item):
        a, sc = item["account"], item["score"]
        return (
            a["parent_id"] == bellhaven_id,
            (a.get("lifetime_revenue") or 0) > 0,
            bool(a["parent_id"]),
            a["status"] == "Active",
            sc,
        )
    return sorted(group, key=key, reverse=True)[0]


# ---------- proposal builders ----------

def website_fields(loc, bellhaven_id):
    return {
        "name": loc["name"],
        "parent_id": bellhaven_id,
        "billing_street": loc["street"],
        "billing_city": loc["city"],
        "billing_state": loc["state"],
        "billing_zip": loc["zip"],
        "care_type": CARE_MAP.get(loc["care_offerings"][0], "") if loc["care_offerings"] else "",
        "phone": loc["phone"],
        "status": "Active",
    }


def field_diffs(loc, acct):
    """Fields on an existing account that disagree with the website (in a way that matters)."""
    d = {}
    # The website is the source of truth for the community's current name, so
    # any difference (rebrand, "of" vs "at", abbreviations) is synced.
    if loc["name"].strip() != (acct["name"] or "").strip():
        d["name"] = loc["name"]
    if loc["phone"] and loc["phone"] != acct.get("phone"):
        d["phone"] = loc["phone"]
    if is_po_box(acct["billing_street"]) or norm_street(loc["street"]) != norm_street(acct["billing_street"]):
        d["billing_street"] = loc["street"]
    if loc["city"].lower() != (acct["billing_city"] or "").lower():
        d["billing_city"] = loc["city"]
    if loc["state"] != acct["billing_state"]:
        d["billing_state"] = loc["state"]
    if loc["zip"] != acct["billing_zip"]:
        d["billing_zip"] = loc["zip"]
    allowed = {CARE_MAP.get(o) for o in loc["care_offerings"]}
    if loc["care_offerings"] and acct.get("care_type") not in allowed:
        d["care_type"] = CARE_MAP.get(loc["care_offerings"][0], "")
    if acct["status"] != "Active":
        d["status"] = "Active"
    return d


def build_proposals(locations, accounts):
    today = date.today().isoformat()
    by_id = {a["account_id"]: a for a in accounts}
    parent = next(a for a in accounts if a["name"] == BELLHAVEN_PARENT_NAME)
    bh = parent["account_id"]

    # Accounts that are already "resolved history" are not live candidates:
    #  - marked duplicate of another account
    #  - preserved for billing with a CHOW pointer to the current account
    #  - parent-company accounts themselves
    live = [a for a in accounts
            if not a.get("duplicate_of_account") and not a.get("chow_current_account")
            and "(Parent Account)" not in a["name"]]

    proposals, claimed = [], set()

    for loc in locations:
        scored = []
        for a in live:
            s, why = score(loc, a)
            scored.append({"account": a, "score": s, "why": why})
        scored.sort(key=lambda x: x["score"], reverse=True)
        same_addr = [x for x in scored if same_place(loc, x["account"])]
        # Fallback when the CRM street is unusable (PO box, typo): same city/state/zip
        # AND (same phone OR near-identical name). Both signals are needed so a
        # different facility in the same zip can't be pulled in by name alone.
        fallback = [x for x in scored
                    if loc["city"].lower() == (x["account"]["billing_city"] or "").lower()
                    and loc["state"] == x["account"]["billing_state"]
                    and loc["zip"] == x["account"]["billing_zip"]
                    and (x["account"].get("phone") == loc["phone"]
                         or name_sim(loc["name"], x["account"]["name"]) >= 0.9)]
        strong = same_addr or fallback
        near_misses = [
            {"account": account_snapshot(x["account"]), "score": x["score"], "why": x["why"]}
            for x in scored[:3] if x not in strong
        ]

        if not strong:
            # ---- NEW: location has no CRM account ----
            fields = website_fields(loc, bh)
            fields["note"] = f"Created {today} by Bellhaven website sync from {loc['url']}"
            proposals.append({
                "type": "create_new",
                "subject": loc["slug"],
                "title": f"Create account: {loc['name']} ({loc['city']}, {loc['state']})",
                "confidence": "high" if not near_misses or near_misses[0]["score"] < 50 else "medium",
                "actions": [{"op": "create", "fields": fields}],
                "evidence": {
                    "website": site_snapshot(loc),
                    "reasoning": "No CRM account at this street address, and no high-scoring candidate. "
                                 "Closest candidates are listed so the reviewer can confirm none is the same facility.",
                    "closest_candidates": near_misses,
                },
            })
            continue

        primary = pick_primary(strong, bh)
        acct = primary["account"]
        claimed.update(x["account"]["account_id"] for x in strong)
        base_evidence = {
            "website": site_snapshot(loc),
            "crm": account_snapshot(acct),
            "match_reasons": primary["why"],
            "match_score": primary["score"],
        }

        # ---- duplicates at the same address ----
        for dup in [x for x in strong if x is not primary]:
            d = dup["account"]
            if (d.get("lifetime_revenue") or 0) > 0:
                proposals.append({
                    "type": "needs_review",
                    "subject": d["account_id"],
                    "title": f"Possible duplicate with billing history: {d['name']}",
                    "confidence": "low",
                    "actions": [{"op": "patch", "account_id": d["account_id"], "fields": {
                        "status": "Needs Review",
                        "note": f"{today}: same address as {acct['account_id']} ({loc['name']}); has revenue history, "
                                f"so not auto-marked duplicate. Billing should confirm."}}],
                    "evidence": {**base_evidence, "duplicate": account_snapshot(d)},
                })
                continue
            proposals.append({
                "type": "duplicate",
                "subject": d["account_id"],
                "title": f"Mark duplicate: {d['name']} -> {loc['name']}",
                "confidence": "high",
                "actions": [{"op": "patch", "account_id": d["account_id"], "fields": {
                    "duplicate_of_account": acct["account_id"],
                    "status": "Inactive",
                    "note": f"{today}: duplicate of {acct['account_id']} - same street address as Bellhaven "
                            f"community '{loc['name']}' ({loc['url']}). Old operator record; no billing history."}}],
                "evidence": {**base_evidence, "duplicate": account_snapshot(d),
                             "reasoning": "Same street, city and state as the surviving account. Survivor chosen by: "
                                          "already under Bellhaven > has revenue > has a parent > Active > match score."},
            })

        diffs = field_diffs(loc, acct)
        wrong_parent = acct["parent_id"] != bh

        if wrong_parent and has_chow_history(acct):
            # ---- SOP: CHOW. Keep old account untouched, create new one, link old -> new ----
            fields = website_fields(loc, bh)
            fields["note"] = (f"Created {today} via change of ownership from {acct['account_id']} "
                              f"({acct['parent_name'] or 'no parent'}). Source: {loc['url']}")
            proposals.append({
                "type": "chow",
                "subject": acct["account_id"],
                "title": f"Change of ownership: {acct['name']} ({acct['parent_name'] or 'no parent'} -> Bellhaven)",
                "confidence": "high",
                "actions": [
                    {"op": "create", "fields": fields, "save_as": "new_account"},
                    {"op": "patch", "account_id": acct["account_id"],
                     "fields": {"chow_current_account": "$new_account"}},
                ],
                "evidence": {**base_evidence,
                             "reasoning": f"Belongs under Bellhaven but has lifetime_revenue={acct['lifetime_revenue']} "
                                          f"and outstanding_ar={acct['outstanding_ar']} > 0. Per SOP the old account is "
                                          f"preserved as-is; a new account is created under Bellhaven and the old one "
                                          f"points to it via chow_current_account."},
            })
        elif wrong_parent:
            # ---- re-parent directly (no revenue history or no AR) ----
            fields = {"parent_id": bh, **diffs}
            proposals.append({
                "type": "reparent",
                "subject": acct["account_id"],
                "title": f"Re-parent: {acct['name']} ({acct['parent_name'] or 'no parent'} -> Bellhaven)",
                "confidence": "high",
                "actions": [{"op": "patch", "account_id": acct["account_id"], "fields": fields}],
                "evidence": {**base_evidence, "field_changes": diffs,
                             "reasoning": f"Listed on Bellhaven's website but parented to "
                                          f"'{acct['parent_name'] or 'nothing'}'. CHOW rule does not apply "
                                          f"(revenue={acct['lifetime_revenue']}, AR={acct['outstanding_ar']}), "
                                          f"so the existing account is re-parented directly."},
            })
        elif diffs:
            # ---- right parent, stale fields (outdated name, bad zip, PO box...) ----
            proposals.append({
                "type": "update_fields",
                "subject": acct["account_id"],
                "title": f"Fix fields: {acct['name']}" + (f" -> {diffs['name']}" if "name" in diffs else ""),
                "confidence": "high" if same_place(loc, acct) else "medium",
                "actions": [{"op": "patch", "account_id": acct["account_id"], "fields": diffs}],
                "evidence": {**base_evidence, "field_changes": diffs},
            })
        # else: confident match, nothing to change (reported as "confirmed" by the pipeline)
        else:
            proposals.append({"type": "confirmed", "subject": acct["account_id"],
                              "title": f"Confirmed: {loc['name']}", "actions": [], "evidence": base_evidence})

    # ---- Bellhaven-parented accounts that are no longer on the website ----
    for a in live:
        if a["parent_id"] != bh or a["account_id"] in claimed:
            continue
        successor = next((o for o in live if o["account_id"] != a["account_id"]
                          and o["parent_id"] and o["parent_id"] != bh
                          and norm_street(o["billing_street"]) == norm_street(a["billing_street"])
                          and o["billing_zip"] == a["billing_zip"]), None)
        ev = {"crm": account_snapshot(a)}
        if successor:
            ev["new_operator_account"] = account_snapshot(successor)
            if has_chow_history(a):
                actions = [{"op": "patch", "account_id": a["account_id"],
                            "fields": {"chow_current_account": successor["account_id"]}}]
                why = (f"Not on Bellhaven's website. Same address already exists under "
                       f"'{successor['parent_name']}', so the facility changed hands. Old account has revenue AND "
                       f"AR, so per SOP it is kept as-is and pointed at the existing current account "
                       f"(no need to create a third record).")
                typ = "chow"
            else:
                actions = [{"op": "patch", "account_id": a["account_id"], "fields": {
                    "duplicate_of_account": successor["account_id"], "status": "Inactive",
                    "note": f"{today}: no longer on Bellhaven website; facility now operated under "
                            f"{successor['parent_name']} as {successor['account_id']}."}}]
                why = "Not on Bellhaven's website and the same facility already exists under the new operator."
                typ = "duplicate"
            proposals.append({"type": typ, "subject": a["account_id"],
                              "title": f"Left Bellhaven: {a['name']} -> {successor['parent_name']}",
                              "confidence": "high", "actions": actions,
                              "evidence": {**ev, "reasoning": why}})
        else:
            proposals.append({
                "type": "not_on_website",
                "subject": a["account_id"],
                "title": f"No longer on website: {a['name']} ({a['billing_city']}, {a['billing_state']})",
                "confidence": "medium",
                "actions": [{"op": "patch", "account_id": a["account_id"], "fields": {
                    "status": "Needs Review",
                    "note": f"{today}: under Bellhaven in CRM but not listed on bellhaven website. New operator unknown; "
                            f"sales should confirm ownership before outreach."}}],
                "evidence": {**ev, "reasoning": "Parented to Bellhaven but not found among website locations, and no "
                                                "other operator's account exists at this address. We can't tell who "
                                                "owns it now, so flag it rather than guess a new parent."},
            })
    return proposals
