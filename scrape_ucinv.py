import csv
import re
import time
from playwright.sync_api import sync_playwright

SEARCH_URL = "https://plus.fairfaxcounty.gov/CitizenAccess/Cap/GlobalSearchResults.aspx?QueryText=UCINV-2026"
OUT_CSV = "ucinv_2026_locations.csv"


def extract_work_location(page):
    body = page.locator("body").inner_text()
    idx = body.find("Work Location")
    if idx == -1:
        return ""
    chunk = body[idx: idx + 400]
    lines = [l.strip() for l in chunk.split("\n") if l.strip()]
    # first line after "Work Location" heading is the address line
    if len(lines) > 1:
        return lines[1]
    return ""


def extract_status(page):
    body = page.locator("body").inner_text()
    m = re.search(r"Record Status:\s*(.+)", body)
    return m.group(1).strip() if m else ""


def load_existing():
    results = []
    seen = set()
    try:
        with open(OUT_CSV, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                results.append(row)
                seen.add(row["record_number"])
    except FileNotFoundError:
        pass
    return results, seen


def main():
    results, seen = load_existing()
    print(f"Resuming with {len(results)} already-scraped records.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(ignore_https_errors=True)
        page.goto(SEARCH_URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(1500)

        page_num = 1
        while True:
            print(f"--- List page {page_num} ---")
            rows = page.locator("a[id*='lnkPermitNumber']")
            count = rows.count()
            # collect record numbers on this page (skip header row index 0)
            record_numbers = []
            for i in range(1, count):
                txt = rows.nth(i).inner_text().strip()
                record_numbers.append(txt)

            for rec in record_numbers:
                if not rec.startswith("UCINV-2026"):
                    print(f"  skip non-UCINV record: {rec}")
                    continue
                if rec in seen:
                    continue
                seen.add(rec)

                # re-locate rows fresh each time (DOM may have been replaced)
                target = None
                for attempt in range(4):
                    try:
                        page.wait_for_selector("a[id*='lnkPermitNumber']", timeout=15000)
                    except Exception:
                        pass
                    rows = page.locator("a[id*='lnkPermitNumber']")
                    n = rows.count()
                    for i in range(1, n):
                        try:
                            if rows.nth(i).inner_text().strip() == rec:
                                target = rows.nth(i)
                                break
                        except Exception:
                            continue
                    if target is not None:
                        break
                    page.wait_for_timeout(1500)
                if target is None:
                    print(f"  could not relocate {rec} after retries, skipping")
                    continue

                try:
                    target.click()
                    page.wait_for_load_state("networkidle", timeout=60000)
                    page.wait_for_selector("text=Work Location", timeout=20000)
                    page.wait_for_timeout(600)
                    url = page.url
                    address = extract_work_location(page)
                    status = extract_status(page)
                    print(f"  {rec}: {address} [{status}]")
                    results.append({
                        "record_number": rec,
                        "work_location": address,
                        "status": status,
                        "detail_url": url,
                    })
                    # write incrementally so partial progress is never lost
                    write_csv(results)

                    page.go_back()
                    page.wait_for_load_state("networkidle", timeout=60000)
                    page.wait_for_selector("a[id*='lnkPermitNumber']", timeout=20000)
                    page.wait_for_timeout(600)
                except Exception as e:
                    print(f"  ERROR on {rec}: {e}")
                    # try to recover by reloading the search page fresh and re-paging
                    for recovery_attempt in range(3):
                        try:
                            page.goto(SEARCH_URL, wait_until="networkidle", timeout=60000)
                            page.wait_for_timeout(1200)
                            for _ in range(page_num - 1):
                                page.locator("a:has-text('Next')").first.click()
                                page.wait_for_load_state("networkidle", timeout=60000)
                                page.wait_for_timeout(800)
                            page.wait_for_selector("a[id*='lnkPermitNumber']", timeout=15000)
                            break
                        except Exception as e2:
                            print(f"    recovery attempt {recovery_attempt} failed: {e2}")
                            page.wait_for_timeout(2000)

            # go to next page of results
            next_link = page.locator("a:has-text('Next')")
            if next_link.count() == 0:
                print("No more pages.")
                break
            next_link.first.click()
            page.wait_for_load_state("networkidle", timeout=60000)
            page.wait_for_timeout(1000)
            page_num += 1

            if page_num > 50:  # safety cap
                break

        browser.close()

    write_csv(results)
    print(f"\nDone. {len(results)} records written to {OUT_CSV}")


def write_csv(results):
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["record_number", "work_location", "status", "detail_url"])
        w.writeheader()
        for r in results:
            w.writerow(r)


if __name__ == "__main__":
    main()
