"""Thin client for the CRM sandbox API."""
import json
import urllib.error
import urllib.request

from config import API_BASE, API_TOKEN


class CRMError(Exception):
    pass


def _call(method, path, body=None):
    if not API_TOKEN:
        raise CRMError("CRM_TOKEN environment variable is not set")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API_BASE + path,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {API_TOKEN}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raise CRMError(f"{method} {path} -> {e.code}: {e.read().decode()[:300]}")


def list_accounts():
    out, page = [], 1
    while True:
        r = _call("GET", f"/accounts?page={page}&page_size=200")
        out += r["data"]
        if len(out) >= r.get("total", 0) or not r["data"]:
            return out
        page += 1


def get_account(account_id):
    return _call("GET", f"/accounts/{account_id}")


def update_account(account_id, fields):
    return _call("PATCH", f"/accounts/{account_id}", fields)


def create_account(fields):
    return _call("POST", "/accounts", fields)
