import os

SITE_BASE = os.environ.get("BELLHAVEN_SITE", "https://analyst-assessment-production.up.railway.app")
API_BASE = SITE_BASE + "/api/v1"
# Token comes from the environment so it never gets committed.
API_TOKEN = os.environ.get("CRM_TOKEN", "")
DB_PATH = os.environ.get("REVIEW_DB", "data/review.db")
