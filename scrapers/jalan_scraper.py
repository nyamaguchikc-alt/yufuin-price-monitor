import asyncio
import re
from datetime import date, timedelta
from playwright.async_api import async_playwright

HOTELS = {
    "kai_yufuin": {
        "name": "界 由布院",
        "yadNo": "360321",
        "planCd": "03416947",
        "roomTypeCd": "0518226",
    },
    "enowa_yufuin": {
        "name": "ENOWA YUFUIN",
        "yadNo": "350146",
        "planCd": None,
        "roomTypeCd": None,
    },
}

PLAN_KEYWORDS = {
    "enowa_yufuin": ["ENOWA", "Standard", "半露天"],
}


async def _discover_plan(page, hotel_key: str) -> tuple[str | None, str | None]:
    hotel = HOTELS[hotel_key]
    url = (
        f"https://www.jalan.net/yad{hotel['yadNo']}/plan/"
        f"?screenId=UWW3001&yadNo={hotel['yadNo']}&smlCd=440602&distCd=01"
    )
    await page.goto(url, wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(2000)

    keywords = PLAN_KEYWORDS.get(hotel_key, [])
    links = await page.query_selector_all("a[href*='planCd']")
    for link in links:
        text = await link.inner_text()
        href = await link.get_attribute("href") or ""
        if any(kw in text or kw in href for kw in keywords):
            plan_match = re.search(r"planCd=(\w+)", href)
            room_match = re.search(r"roomTypeCd=(\w+)", href)
            if plan_match and room_match:
                return plan_match.group(1), room_match.group(1)

    # Fallback: first planCd/roomTypeCd found in page
    content = await page.content()
    matches = re.findall(r"planCd=(\w+)[^\"]*roomTypeCd=(\w+)", content)
    if matches:
        return matches[0]
    return None, None


async def _get_monthly_prices(
    page, yadNo: str, planCd: str, roomTypeCd: str, year: int, month: int
) -> dict[str, int]:
    url = (
        f"https://www.jalan.net/uw/uwp3200/uww3201init.do"
        f"?stayYear={year}&stayMonth={month:02d}&stayDay=01"
        f"&yadNo={yadNo}&stayCount=1&roomCount=1&adultNum=2"
        f"&distCd=01&smlCd=440602&roomCrack=200000"
        f"&planCd={planCd}&roomTypeCd={roomTypeCd}"
    )
    await page.goto(url, wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(2000)

    prices = {}

    # Try JSON-like price data embedded in page
    content = await page.content()
    json_matches = re.findall(
        r'"stayDate"\s*:\s*"(\d{4}/\d{2}/\d{2})"[^}]*?"price"\s*:\s*(\d+)', content
    )
    for raw_date, price_str in json_matches:
        d = raw_date.replace("/", "-")
        prices[d] = int(price_str)

    if prices:
        return prices

    # Fallback: scrape calendar cells
    cells = await page.query_selector_all(
        ".planCalendarArea td, [class*='calendar'] td, td[data-date]"
    )
    for cell in cells:
        data_date = await cell.get_attribute("data-date")
        if data_date:
            price_el = await cell.query_selector("[class*='price'], .price")
            if price_el:
                price_text = await price_el.inner_text()
                price_match = re.search(r"([0-9,]+)", price_text)
                if price_match:
                    prices[data_date] = int(price_match.group(1).replace(",", ""))
            continue

        text = await cell.inner_text()
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


async def scrape_jalan_hotel(hotel_key: str) -> dict[str, int | None]:
    hotel = HOTELS[hotel_key]
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

        plan_cd = hotel["planCd"]
        room_type_cd = hotel["roomTypeCd"]

        if plan_cd is None:
            plan_cd, room_type_cd = await _discover_plan(page, hotel_key)
            if plan_cd:
                HOTELS[hotel_key]["planCd"] = plan_cd
                HOTELS[hotel_key]["roomTypeCd"] = room_type_cd
            else:
                print(f"  [warn] {hotel_key}: planCd not found, skipping")
                await browser.close()
                return {}

        # Scrape 7 months (covers 180 days from today)
        for offset in range(7):
            first = (today.replace(day=1) + timedelta(days=32 * offset)).replace(day=1)
            monthly = await _get_monthly_prices(
                page, hotel["yadNo"], plan_cd, room_type_cd, first.year, first.month
            )
            all_prices.update(monthly)
            await asyncio.sleep(2)

        await browser.close()

    return {
        d: p
        for d, p in all_prices.items()
        if today.isoformat() <= d <= end_date.isoformat()
    }
