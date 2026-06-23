import asyncio
import re
from datetime import date, timedelta
from playwright.async_api import async_playwright

HOTEL_ID = "00002470"
TARGET_PLAN_KEYWORDS = ["亀の井別荘の洋室", "季節の会席"]
TARGET_ROOM_KEYWORDS = ["園林", "本館洋室ツイン"]


async def _extract_prices_from_page(page, year: int, month: int) -> dict[str, int]:
    prices = {}
    content = await page.content()

    # Pattern 1: JSON embedded data
    json_matches = re.findall(
        r'"date"\s*:\s*"(\d{4}-\d{2}-\d{2})"[^}]{0,200}?"price"\s*:\s*(\d+)', content
    )
    for d, p in json_matches:
        prices[d] = int(p)

    if prices:
        return prices

    # Pattern 2: data attributes on calendar cells
    cells = await page.query_selector_all("[data-date], [data-ymd]")
    for cell in cells:
        raw = await cell.get_attribute("data-date") or await cell.get_attribute("data-ymd")
        if not raw:
            continue
        text = await cell.inner_text()
        price_match = re.search(r"([0-9,]+)", text)
        if price_match:
            prices[raw[:10]] = int(price_match.group(1).replace(",", ""))

    if prices:
        return prices

    # Pattern 3: parse visible price cells from calendar grid
    price_els = await page.query_selector_all(
        "[class*='calendar'] td, [class*='Calendar'] td"
    )
    for el in price_els:
        text = await el.inner_text()
        date_match = re.search(r"\b(\d{1,2})\b", text)
        price_match = re.search(r"([0-9,]+)円", text)
        if date_match and price_match:
            day = int(date_match.group(1))
            try:
                d = date(year, month, day).isoformat()
                prices[d] = int(price_match.group(1).replace(",", ""))
            except ValueError:
                pass

    return prices


async def scrape_ikyu_kamenoi() -> dict[str, int | None]:
    today = date.today()
    end_date = today + timedelta(days=180)
    all_prices: dict[str, int | None] = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()

        base_url = f"https://www.ikyu.com/{HOTEL_ID}/"

        for offset in range(7):
            first = (today.replace(day=1) + timedelta(days=32 * offset)).replace(day=1)
            url = (
                f"{base_url}?adc=1&discsort=1&lc=1&ppc=2&rc=1&si=1"
                f"&st={first.strftime('%Y%m%d')}&top=plans"
            )
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(3000)

            # Try to find target plan and click calendar view
            plan_links = await page.query_selector_all("a, button")
            for link in plan_links:
                text = await link.inner_text()
                if any(kw in text for kw in TARGET_PLAN_KEYWORDS):
                    try:
                        await link.click(timeout=3000)
                        await page.wait_for_timeout(2000)
                    except Exception:
                        pass
                    break

            monthly = await _extract_prices_from_page(page, first.year, first.month)
            all_prices.update(monthly)
            await asyncio.sleep(2)

        await browser.close()

    return {
        d: p
        for d, p in all_prices.items()
        if today.isoformat() <= d <= end_date.isoformat()
    }
