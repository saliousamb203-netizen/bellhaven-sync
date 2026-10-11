"""Scrape every Bellhaven community from the public website.

Walks the paginated /communities directory AND any community links found on
the homepage (the homepage can announce new communities before the directory
lists them), then reads each community's detail page.
"""
import html
import json
import re
import urllib.request

from config import SITE_BASE

UA = {"User-Agent": "Mozilla/5.0 (Bellhaven sync bot)"}


def fetch(path):
    req = urllib.request.Request(SITE_BASE + path, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def community_links(page_html):
    return re.findall(r'href="(/communities/[a-z0-9\-]+)"', page_html)


def directory_links():
    links, page = [], 1
    while True:
        body = fetch(f"/communities?page={page}")
        links += community_links(body)
        m = re.search(r"Page (\d+) of (\d+)", body)
        if not m or int(m.group(1)) >= int(m.group(2)):
            break
        page += 1
    return links


def clean(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def parse_detail(path):
    body = fetch(path)
    name = clean(re.search(r"<h1>(.*?)</h1>", body, re.S).group(1))
    fields = dict(
        (clean(k).lower(), v)
        for k, v in re.findall(r"<dt>(.*?)</dt><dd>(.*?)</dd>", body, re.S)
    )
    street, cityline = [clean(p) for p in re.split(r"<br\s*/?>", fields["address"])]
    city, rest = cityline.rsplit(",", 1)
    state, zip_ = rest.split()
    offerings = [clean(b) for b in re.findall(r'<span class="badge">(.*?)</span>', fields.get("care offerings", ""))]
    return {
        "slug": path.rsplit("/", 1)[1],
        "url": SITE_BASE + path,
        "name": name,
        "street": street,
        "city": city.strip(),
        "state": state,
        "zip": zip_,
        "care_offerings": offerings,
        "administrator": clean(fields.get("administrator", "")),
        "phone": clean(fields.get("phone", "")),
    }


def scrape():
    dir_links = directory_links()
    home_links = community_links(fetch("/"))
    all_links = list(dict.fromkeys(dir_links + home_links))  # dedupe, keep order
    locations = []
    for path in all_links:
        loc = parse_detail(path)
        loc["source"] = "directory" if path in dir_links else "homepage_only"
        locations.append(loc)
    return locations


if __name__ == "__main__":
    locs = scrape()
    json.dump(locs, open("data/website_locations.json", "w"), indent=1)
    print(f"Scraped {len(locs)} locations")
    for l in locs:
        print(f"  {l['name']:<55} {l['city']}, {l['state']} {l['zip']}  [{l['source']}]")
