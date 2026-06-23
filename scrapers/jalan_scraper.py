import asyncio
import json
import re
from datetime import date, timedelta
from pathlib import Path
from playwright.async_api import async_playwright

DEBUG_DIR = Path(__file__).parent.parent / "data" / "debug"

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

LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
]


def _extract_prices_from_json(obj, prices: dict, year: int, month: int):
    """JSONオブジェクトを再帰的に探索してdate+priceペアを抽出"""
    if isinstance(obj, dict):
        stay = (
            obj.get("stayDate") or obj.get("date") or obj.get("ymd") or obj.get("checkInDate")
        )
        price = (
            obj.get("price") or obj.get("minPrice") or obj.get("planPrice") or obj.get("amount")
        )
        if stay and price:
            raw = str(stay).replace("/", "-")[:10]
            try:
                d = date.fromisoformat(raw)
                if d.year == year and d.month == month:
                    prices[raw] = int(str(price).replace(",", ""))
            except (ValueError, TypeError):
                pass
        for v in obj.values():
            _extract_prices_from_json(v, prices, year, month)
    elif isinstance(obj, list):
        for item in obj:
            _extract_prices_from_json(item, prices, year, month)


async def _discover_plan(page, hotel_key: str) -> tuple[str | None, str | None]:
    hotel = HOTELS[hotel_key]
    url = (
        f"https://www.jalan.net/yad{hotel['yadNo']}/plan/"
        f"?screenId=UWW3001&yadNo={hotel['yadNo']}&smlCd=440602&distCd=01"
    )
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(4000)

    content = await page.content()
    matches = re.findall(r"planCd=(\w+)[^\"' ]*roomTypeCd=(\w+)", content)
    if matches:
        return matches[0]

    links = await page.query_selector_all("a[href*='planCd']")
    for link in links:
        href = await link.get_attribute("href") or ""
        plan_m = re.search(r"planCd=(\w+)", href)
        room_m = re.search(r"roomTypeCd=(\w+)", href)
        if plan_m and room_m:
            return plan_m.group(1), room_m.group(1)

    return None, None


async def _get_monthly_prices(
    page, yadNo: str, planCd: str, roomTypeCd: str, year: int, month: int
) -> dict[str, int]:
    prices: dict[str, int] = {}
    intercepted: list[dict] = []

    async def handle_response(resp):
        ct = resp.headers.get("content-type", "")
        if "json" in ct:
            try:
                body = await resp.json()
                intercepted.append(body)
            except Exception:
                pass

    page.on("response", handle_response)

    url = (
        f"https://www.jalan.net/uw/uwp3200/uww3201init.do"
        f"?stayYear={year}&stayMonth={month:02d}&stayDay=01"
        f"&yadNo={yadNo}&stayCount=1&roomCount=1&adultNum=2"
        f"&distCd=01&smlCd=440602&roomCrack=200000"
        f"&planCd={planCd}&roomTypeCd={roomTypeCd}"
    )
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(5000)

    page.remove_listener("response", handle_response)

    # ① ネットワーク傍受で取得したJSONを解析
    for body in intercepted:
        _extract_prices_from_json(body, prices, year, month)
    if prices:
        return prices

    # ② ページHTML内の複数パターンで正規表現検索
    content = await page.content()
    for pattern in [
        r'"stayDate"\s*:\s*"(\d{4}[/\-]\d{2}[/\-]\d{2})"[^}]{0,300}"price"\s*:\s*(\d+)',
        r'"date"\s*:\s*"(\d{4}[/\-]\d{2}[/\-]\d{2})"[^}]{0,300}"price"\s*:\s*(\d+)',
        r'"ymd"\s*:\s*"(\d{4}[/\-]\d{2}[/\-]\d{2})"[^}]{0,300}"price"\s*:\s*(\d+)',
        r'(\d{4}[/\-]\d{2}[/\-]\d{2})[^0-9]{1,20}([1-9][0-9]{4,6})(?:円|")',
    ]:
        for m in re.finditer(pattern, content):
            raw = m.group(1).replace("/", "-")
            try:
                d = date.fromisoformat(raw)
                if d.year == year and d.month == month:
                    prices[raw] = int(m.group(2).replace(",", ""))
            except (ValueError, IndexError):
                pass
        if prices:
            return prices

    # ③ DOMセレクタ（6種類）で要素を走査
    for sel in [
        "td[data-date]", "[data-ymd]", "[data-checkin]",
        "[class*='calendar'] td", "[class*='Calendar'] td", ".planCalendarList td",
    ]:
        cells = await page.query_selector_all(sel)
        for cell in cells:
            raw_date = (
                await cell.get_attribute("data-date")
                or await cell.get_attribute("data-ymd")
            )
            text = await cell.inner_text()
            price_m = re.search(r"([1-9][0-9,]{4,})", text)
            if price_m:
                price_val = int(price_m.group(1).replace(",", ""))
                if raw_date:
                    key = raw_date[:10].replace("/", "-")
                    try:
                        date.fromisoformat(key)
                        prices[key] = price_val
                    except ValueError:
                        pass
                else:
                    day_m = re.search(r"\b(\d{1,2})\b", text)
                    if day_m:
                        try:
                            d = date(year, month, int(day_m.group(1)))
                            prices[d.isoformat()] = price_val
                        except ValueError:
                            pass
        if prices:
            return prices

    # どの手法でも取れなかった場合はデバッグ用HTMLを保存
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    debug_file = DEBUG_DIR / f"jalan_{yadNo}_{year}{month:02d}.html"
    debug_file.write_text(content, encoding="utf-8")
    print(f"  [debug] No prices found, saved HTML → {debug_file}")

    return prices


async def scrape_jalan_hotel(hotel_key: str) -> dict[str, int | None]:
    hotel = HOTELS[hotel_key]
    today = date.today()
    end_date = today + timedelta(days=180)
    all_prices: dict[str, int | None] = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=LAUNCH_ARGS)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="ja-JP",
        )
        await context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        page = await context.new_page()

        plan_cd = hotel["planCd"]
        room_type_cd = hotel["roomTypeCd"]

        if plan_cd is None:
            print(f"  [{hotel_key}] planCd 探索中...")
            plan_cd, room_type_cd = await _discover_plan(page, hotel_key)
            if plan_cd:
                HOTELS[hotel_key]["planCd"] = plan_cd
                HOTELS[hotel_key]["roomTypeCd"] = room_type_cd
                print(f"  [{hotel_key}] planCd={plan_cd} roomTypeCd={room_type_cd}")
            else:
                print(f"  [{hotel_key}] planCd not found, skipping")
                await browser.close()
                return {}

        for offset in range(7):
            first = (today.replace(day=1) + timedelta(days=32 * offset)).replace(day=1)
            print(f"  [{hotel_key}] {first.year}/{first.month:02d} 取得中...")
            monthly = await _get_monthly_prices(
                page, hotel["yadNo"], plan_cd, room_type_cd, first.year, first.month
            )
            print(f"  [{hotel_key}] {first.year}/{first.month:02d} → {len(monthly)} 件")
            all_prices.update(monthly)
            await asyncio.sleep(2)

        await browser.close()

    return {
        d: v
        for d, v in all_prices.items()
        if today.isoformat() <= d <= end_date.isoformat()
    }
