"""Screenshot + smoke-test the demo dashboard with Playwright (Chromium).

    python3 tests/demo/run_demo.py &      # serves on :7777
    python3 tests/demo/shoot.py [OUT_DIR]

Exits non-zero on any console error / page error or failed interaction check.
"""
from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright

URL = os.environ.get("DH_URL", "http://127.0.0.1:7777/")
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/claude-0/shots"
os.makedirs(OUT, exist_ok=True)
errors: list[str] = []
known: list[str] = []
# Known server-side issue (diskhealth/server.py CSP lacks `img-src 'self' data:`), reported, not a page bug.
KNOWN = ("Refused to load the image 'data:image/svg+xml",)
failures: list[str] = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def new_page(browser, width, scheme, height=900):
    ctx = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme,
                              device_scale_factor=1 if width > 800 else 2)
    page = ctx.new_page()
    def on_console(m):
        if m.type == "error":
            (known if m.text.startswith(KNOWN) else errors).append(f"[{width}/{scheme}] console: {m.text[:160]}")
    page.on("console", on_console)
    page.on("pageerror", lambda e: errors.append(f"[{width}/{scheme}] pageerror: {e}"))
    page.goto(URL)
    page.wait_for_selector(".kpi.hero")
    page.wait_for_selector(".tree-row")
    page.wait_for_timeout(300)
    return page


with sync_playwright() as p:
    browser = p.chromium.launch()

    for scheme in ("light", "dark"):
        page = new_page(browser, 1440, scheme)
        page.screenshot(path=f"{OUT}/desktop-{scheme}.png", full_page=True)
        page.context.close()

    page = new_page(browser, 390, "light", 844)
    sw = page.evaluate("document.documentElement.scrollWidth")
    check(sw <= 390, f"no horizontal page scroll at 390px (scrollWidth={sw})")
    page.screenshot(path=f"{OUT}/mobile-light.png", full_page=True)
    page.context.close()
    page = new_page(browser, 390, "dark", 844)
    page.screenshot(path=f"{OUT}/mobile-dark.png", full_page=True)
    page.context.close()

    # ---- interactions (desktop, light)
    page = new_page(browser, 1440, "light")
    # chart tooltip
    page.locator("#freeChart").scroll_into_view_if_needed()
    page.wait_for_timeout(400)  # smooth scrolling
    box = page.locator("#freeChart svg").bounding_box()
    page.mouse.move(box["x"] + box["width"] * 0.6, box["y"] + box["height"] / 2)
    page.wait_for_timeout(150)
    op = page.eval_on_selector("#tt", "e => getComputedStyle(e).opacity")
    check(float(op) > 0.9 and "free" in page.inner_text("#tt"), f"chart tooltip visible: {page.inner_text('#tt')!r}")
    clip = {"x": box["x"] - 10, "y": box["y"] - 50, "width": box["width"] + 20, "height": box["height"] + 60}
    page.screenshot(path=f"{OUT}/chart-hover.png", clip=clip)
    page.mouse.move(box["x"] + box["width"] - 2, box["y"] + 20)
    page.wait_for_timeout(150)
    page.screenshot(path=f"{OUT}/chart-hover-right.png", clip=clip)
    tt = page.locator("#tt").bounding_box()
    op = page.eval_on_selector("#tt", "e => getComputedStyle(e).opacity")
    check(float(op) > 0.9 and tt["x"] + tt["width"] <= box["x"] + box["width"] + 1 and tt["y"] >= box["y"] - 60,
          "tooltip visible and inside chart at top-right edge")

    # growth toggle
    n7 = page.locator("#growthBody tbody tr").count()
    page.click("#growthSeg button[data-d='30']")
    sub30 = page.inner_text("#growthSub")
    n30 = page.locator("#growthBody tbody tr").count()
    check(page.locator("#growthSeg button.on").get_attribute("data-d") == "30", f"growth 30d toggle ({n7} -> {n30} rows, {sub30})")
    page.click("#growthSeg button[data-d='7']")

    # apps toggle
    nu = page.locator("#appsBody tbody tr").count()
    page.click("#appsSeg button[data-f='all']")
    na = page.locator("#appsBody tbody tr").count()
    check(na > nu > 0, f"apps toggle unused={nu} all={na}")
    page.click("#appsSeg button[data-f='unused']")

    # tree drill-down
    page.locator(".tree-row").first.click()
    page.wait_for_timeout(250)
    crumbs = page.inner_text("#crumbs")
    check("All roots" in crumbs and "~" in crumbs, f"crumbs after root click: {crumbs!r}")
    page.locator(".tree-row", has_text="Library").first.click()
    page.wait_for_timeout(250)
    page.locator(".tree-row", has_text="Containers").first.click()
    page.wait_for_timeout(250)
    crumbs = "".join(page.inner_text("#crumbs").split())
    check(crumbs.endswith("~/Library/Containers"), f"deep crumbs: {crumbs!r}")
    page.locator("#space").screenshot(path=f"{OUT}/tree-deep.png")
    page.locator("#crumbs a", has_text="Library").first.click()
    page.wait_for_timeout(250)
    check("".join(page.inner_text("#crumbs").split()) == "Allroots/~/Library", f"crumb back: {page.inner_text('#crumbs')!r}")
    page.locator("#crumbs a", has_text="All roots").click()
    page.wait_for_timeout(250)
    check(page.locator(".tree-row").count() == 4, "All roots shows 4 roots")
    page.locator(".tree-row", has_text="/opt/homebrew").click()
    page.wait_for_timeout(250)
    page.locator(".tree-row", has_text="Cellar").click()
    page.wait_for_timeout(250)
    c = "".join(page.inner_text("#crumbs").split())
    check(c == "Allroots//opt/homebrew/Cellar", f"non-home root crumbs: {c!r}")
    page.locator("#crumbs a", has_text="All roots").click()
    page.wait_for_timeout(250)
    # growth "show more"
    before = page.locator("#growthBody tbody tr").count()
    page.click("#growthMore")
    after = page.locator("#growthBody tbody tr").count()
    check(after > before, f"growth show more ({before} -> {after})")
    page.click("#growthMore")
    # Explore from growth table -> tree
    page.locator("#growthBody [data-tree]").nth(1).click()
    page.wait_for_timeout(300)
    check("docker" in page.inner_text("#crumbs").lower() or page.locator("#tree .empty").count() == 1,
          f"explore from growth: {page.inner_text('#crumbs')!r}")

    # clean confirm -> cancel
    page.locator("[data-clean]").first.click()
    page.wait_for_selector("#confirm[open]")
    page.locator("#confirm").screenshot(path=f"{OUT}/confirm.png")
    page.click("#cfCancel")
    page.wait_for_timeout(150)
    check(page.locator("#confirm[open]").count() == 0, "confirm cancels")
    check(not page.locator("#jobs").is_visible(), "no job started on cancel")
    # Escape also cancels
    page.locator("[data-uninstall]").first.click()
    page.wait_for_selector("#confirm[open]")
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    check(page.locator("#confirm[open]").count() == 0 and not page.locator("#jobs").is_visible(), "Escape cancels")

    # confirm against stubbed actions -> live job drawer
    page.locator("[data-clean]").first.click()
    page.wait_for_selector("#confirm[open]")
    page.click("#cfOk")
    page.wait_for_selector("#jobs.open")
    page.wait_for_timeout(900)
    first = page.inner_text("#jobLog").count("\n")
    page.wait_for_timeout(1200)
    later = page.inner_text("#jobLog").count("\n")
    check(later > first, f"job log streams ({first} -> {later} lines)")
    page.screenshot(path=f"{OUT}/job-drawer.png")
    page.wait_for_timeout(2500)
    check("done" in page.get_attribute("#jobDot", "class"), "job ends done")
    page.click("#jobClose")

    page.click("#scanBtn")
    page.wait_for_timeout(1500)
    check(page.locator("#jobs.open").count() == 1, "scan button opens drawer")
    page.context.close()

    # mobile interactions
    page = new_page(browser, 390, "light", 844)
    page.locator("[data-clean]").first.click()
    page.wait_for_selector("#confirm[open]")
    page.screenshot(path=f"{OUT}/mobile-confirm.png")
    page.click("#cfCancel")
    page.context.close()
    browser.close()

if known:
    print(f"known (not failing): {len(known)}x favicon blocked by server CSP")
print("\n".join(errors) or "no console errors")
if errors or failures:
    sys.exit(1)
