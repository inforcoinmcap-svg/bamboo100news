import logging
import os
import asyncio
import httpx
from datetime import datetime, time
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from telegram import Bot
from groq import Groq

# ============================================================
BOT_TOKEN = os.environ.get("NEWS_BOT_TOKEN", "")
CHANNEL_ID = int(os.environ.get("NEWS_CHANNEL_ID", "0"))
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")  # dùng lại biến này cho Groq key
# ============================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# Tránh đăng lại
posted_events = set()
posted_news = set()

WATCH_LIST = {
    "USD": ["high"],
    "CNY": ["high", "medium"],
}

CURRENCY_FLAGS = {
    "USD": "🇺🇸", "CNY": "🇨🇳", "EUR": "🇪🇺",
    "GBP": "🇬🇧", "JPY": "🇯🇵", "AUD": "🇦🇺",
}


def get_vn_time():
    return datetime.now(VN_TZ)


def call_gemini(prompt, retries=3):
    client = Groq(api_key=GEMINI_API_KEY)
    for attempt in range(retries):
        try:
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1000
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            if "429" in str(e):
                wait = (attempt + 1) * 10
                logger.warning(f"Groq rate limit, waiting {wait}s...")
                import time
                time.sleep(wait)
            else:
                logger.error(f"Groq error: {e}")
                raise e
    return None


# ═══════════════════════════════════════════
# PHẦN 1: FOREXFACTORY
# ═══════════════════════════════════════════

async def fetch_forexfactory():
    url = "https://www.forexfactory.com/calendar"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.text
    except Exception as e:
        logger.error(f"ForexFactory fetch error: {e}")
        return None


def parse_calendar(html):
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    events = []
    calendar_table = soup.find("table", class_="calendar__table")
    if not calendar_table:
        return []

    rows = calendar_table.find_all("tr", class_="calendar__row")
    current_date = ""

    for row in rows:
        try:
            date_cell = row.find("td", class_="calendar__date")
            if date_cell and date_cell.text.strip():
                current_date = date_cell.text.strip()

            time_cell = row.find("td", class_="calendar__time")
            if not time_cell:
                continue
            event_time = time_cell.text.strip()

            currency_cell = row.find("td", class_="calendar__currency")
            if not currency_cell:
                continue
            currency = currency_cell.text.strip()
            if currency not in WATCH_LIST:
                continue

            impact_cell = row.find("td", class_="calendar__impact")
            if not impact_cell:
                continue
            impact_span = impact_cell.find("span")
            if not impact_span:
                continue
            impact_class = impact_span.get("class", [])
            impact = "low"
            if any("high" in c for c in impact_class):
                impact = "high"
            elif any("medium" in c for c in impact_class):
                impact = "medium"
            if impact not in WATCH_LIST.get(currency, []):
                continue

            event_cell = row.find("td", class_="calendar__event")
            if not event_cell:
                continue
            event_name = event_cell.text.strip()

            actual_cell = row.find("td", class_="calendar__actual")
            forecast_cell = row.find("td", class_="calendar__forecast")
            previous_cell = row.find("td", class_="calendar__previous")

            actual = actual_cell.text.strip() if actual_cell else ""
            forecast = forecast_cell.text.strip() if forecast_cell else ""
            previous = previous_cell.text.strip() if previous_cell else ""

            event_id = f"{current_date}_{event_time}_{currency}_{event_name}"
            events.append({
                "id": event_id,
                "date": current_date,
                "time": event_time,
                "currency": currency,
                "impact": impact,
                "name": event_name,
                "actual": actual,
                "forecast": forecast,
                "previous": previous,
            })
        except Exception as e:
            logger.error(f"Parse row error: {e}")
            continue
    return events


async def analyze_forex_event(event):
    try:
        flag = CURRENCY_FLAGS.get(event["currency"], "")
        actual = event["actual"]
        forecast = event["forecast"]
        previous = event["previous"]

        try:
            act_val = float(actual.replace("%", "").replace("K", "000").replace("M", "000000"))
            fct_val = float(forecast.replace("%", "").replace("K", "000").replace("M", "000000"))
            comparison = "cao hơn dự báo" if act_val > fct_val else ("thấp hơn dự báo" if act_val < fct_val else "đúng dự báo")
        except:
            comparison = "so với dự báo"

        prompt = f"""Bạn là chuyên gia phân tích thị trường hàng hoá phái sinh.

Tin kinh tế vừa công bố:
- Tên: {event['name']} ({event['currency']} {flag})
- Actual: {actual} ({comparison})
- Dự báo: {forecast} | Trước đó: {previous}

Viết 1-2 câu phân tích tác động ngắn hạn đến thị trường hàng hoá.
Yêu cầu: tiếng Việt, súc tích, đề cập mã hàng hoá cụ thể nếu có liên quan, không dùng template cứng."""

        analysis = await asyncio.to_thread(call_gemini, prompt)
        return analysis
    except Exception as e:
        logger.error(f"Groq forex error: {e}")
        return None


def format_forex_message(event, analysis):
    flag = CURRENCY_FLAGS.get(event["currency"], "")
    actual = event["actual"]
    forecast = event["forecast"]
    previous = event["previous"]

    try:
        act_val = float(actual.replace("%", "").replace("K", "000").replace("M", "000000").replace(",", ""))
        fct_val = float(forecast.replace("%", "").replace("K", "000").replace("M", "000000").replace(",", ""))
        result_icon = "📈" if act_val > fct_val else ("📉" if act_val < fct_val else "➡️")
        result_text = "Cao hơn dự báo" if act_val > fct_val else ("Thấp hơn dự báo" if act_val < fct_val else "Đúng dự báo")
    except:
        result_icon = "📊"
        result_text = ""

    lines = [
        f"⚡️ BREAKING NEWS | {event['name']} {flag}",
        f"",
        f"📌 Actual: {actual}  {result_icon} {result_text}",
        f"📊 Dự báo: {forecast}",
        f"📋 Trước đó: {previous}",
        f"",
    ]
    if analysis:
        lines.append(f"👉 {analysis}")
    return "\n".join(lines)


async def check_forex_news(context=None):
    logger.info("Checking ForexFactory...")
    html = await fetch_forexfactory()
    if not html:
        return
    events = parse_calendar(html)
    bot = Bot(token=BOT_TOKEN)
    for event in events:
        if not event["actual"]:
            continue
        if event["id"] in posted_events:
            continue
        analysis = await analyze_forex_event(event)
        if analysis is None:
            analysis = "Số liệu vừa được công bố, thị trường đang phản ứng."
        message = format_forex_message(event, analysis)
        try:
            await bot.send_message(chat_id=CHANNEL_ID, text=message, parse_mode=None)
            posted_events.add(event["id"])
            logger.info(f"Posted forex: {event['name']}")
            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Telegram error: {e}")


# ═══════════════════════════════════════════
# PHẦN 2: EIA TỒN KHO DẦU & KHÍ
# ═══════════════════════════════════════════

eia_posted = set()

async def fetch_eia_petroleum():
    url = "https://www.eia.gov/petroleum/supply/weekly/"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            soup = BeautifulSoup(response.text, "html.parser")
            return soup.get_text()[:3000] if soup else None
    except Exception as e:
        logger.error(f"EIA petroleum error: {e}")
        return None


async def fetch_eia_gas():
    url = "https://www.eia.gov/naturalgas/storage/dashboard/"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            soup = BeautifulSoup(response.text, "html.parser")
            return soup.get_text()[:3000] if soup else None
    except Exception as e:
        logger.error(f"EIA gas error: {e}")
        return None


async def analyze_eia(data_text, data_type):
    try:
        type_name = "tồn kho dầu thô" if data_type == "oil" else "tồn kho khí tự nhiên"
        prompt = f"""Bạn là chuyên gia phân tích thị trường năng lượng.

Dữ liệu EIA {type_name} vừa công bố:
{data_text[:1500]}

Nếu có số liệu tồn kho cụ thể, hãy:
1. Tóm tắt số liệu chính (actual vs dự báo nếu có)
2. Viết 1-2 câu phân tích tác động đến giá {type_name}

Nếu không tìm thấy số liệu cụ thể, trả về "NO_DATA".
Trả lời bằng tiếng Việt, súc tích."""

        result = await asyncio.to_thread(call_gemini, prompt)
        if not result or "NO_DATA" in result:
            return None
        return result
    except Exception as e:
        logger.error(f"EIA analyze error: {e}")
        return None


async def check_eia_news(context=None):
    now = get_vn_time()
    bot = Bot(token=BOT_TOKEN)

    if now.weekday() == 2 and now.hour >= 21:
        date_key = f"eia_oil_{now.strftime('%Y-%m-%d')}"
        if date_key not in eia_posted:
            data = await fetch_eia_petroleum()
            if data:
                analysis = await analyze_eia(data, "oil")
                if analysis:
                    msg = f"⛽ *Báo cáo tồn kho dầu thô EIA*\n\n{analysis}"
                    try:
                        await bot.send_message(chat_id=CHANNEL_ID, text=msg, parse_mode="Markdown")
                        eia_posted.add(date_key)
                        logger.info("Posted EIA oil")
                    except Exception as e:
                        logger.error(f"EIA oil post error: {e}")

    if now.weekday() == 3 and now.hour >= 21:
        date_key = f"eia_gas_{now.strftime('%Y-%m-%d')}"
        if date_key not in eia_posted:
            data = await fetch_eia_gas()
            if data:
                analysis = await analyze_eia(data, "gas")
                if analysis:
                    msg = f"🔥 *Báo cáo tồn kho khí tự nhiên EIA*\n\n{analysis}"
                    try:
                        await bot.send_message(chat_id=CHANNEL_ID, text=msg, parse_mode="Markdown")
                        eia_posted.add(date_key)
                        logger.info("Posted EIA gas")
                    except Exception as e:
                        logger.error(f"EIA gas post error: {e}")


# ═══════════════════════════════════════════
# PHẦN 3: USDA DOANH SỐ XUẤT KHẨU
# ═══════════════════════════════════════════

usda_posted = set()

async def fetch_usda_export_sales():
    url = "https://apps.fas.usda.gov/export-sales/esrd1.html"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            soup = BeautifulSoup(response.text, "html.parser")
            return soup.get_text()[:3000] if soup else None
    except Exception as e:
        logger.error(f"USDA export error: {e}")
        return None


async def analyze_usda(data_text):
    try:
        prompt = f"""Bạn là chuyên gia phân tích thị trường nông sản.

Báo cáo doanh số xuất khẩu nông sản USDA tuần này:
{data_text[:1500]}

Nếu có số liệu cụ thể, hãy:
1. Tóm tắt các mặt hàng chính (đậu tương, ngô, lúa mỳ)
2. So sánh với tuần trước nếu có
3. Viết 1-2 câu phân tích tác động đến giá

Nếu không tìm thấy số liệu cụ thể, trả về "NO_DATA".
Trả lời bằng tiếng Việt, súc tích, đề cập mã ZSE/ZCE/ZWA nếu liên quan."""

        result = await asyncio.to_thread(call_gemini, prompt)
        if not result or "NO_DATA" in result:
            return None
        return result
    except Exception as e:
        logger.error(f"USDA analyze error: {e}")
        return None


async def check_usda_news(context=None):
    now = get_vn_time()
    if now.weekday() != 3:
        return
    if now.hour < 20:
        return

    date_key = f"usda_export_{now.strftime('%Y-%m-%d')}"
    if date_key in usda_posted:
        return

    data = await fetch_usda_export_sales()
    if not data:
        return

    analysis = await analyze_usda(data)
    if not analysis:
        return

    bot = Bot(token=BOT_TOKEN)
    msg = f"🌾 *Báo cáo Doanh số Xuất khẩu Nông sản USDA*\n\n{analysis}"
    try:
        await bot.send_message(chat_id=CHANNEL_ID, text=msg, parse_mode="Markdown")
        usda_posted.add(date_key)
        logger.info("Posted USDA export sales")
    except Exception as e:
        logger.error(f"USDA post error: {e}")


# ═══════════════════════════════════════════
# PHẦN 4: TIN MẨU HÀNG HOÁ
# ═══════════════════════════════════════════

async def fetch_from_source(url, base_url=""):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://www.google.com/",
    }
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            if response.status_code != 200:
                logger.warning(f"Failed {url}: {response.status_code}")
                return []
            soup = BeautifulSoup(response.text, "html.parser")
            articles = []
            for el in soup.find_all(["h2", "h3", "h4"])[:30]:
                title = el.text.strip()
                link_el = el.find("a", href=True) or el.find_parent("a", href=True)
                link = ""
                if link_el:
                    link = link_el.get("href", "")
                    if link and not link.startswith("http"):
                        link = f"{base_url}{link}"
                if title and len(title) > 25 and len(title) < 200:
                    articles.append({"title": title, "link": link})
            return articles[:15]
    except Exception as e:
        logger.error(f"Fetch error {url}: {e}")
        return []


async def fetch_commodity_news_sources():
    all_articles = []

    articles1 = await fetch_from_source(
        "https://www.nasdaq.com/market-activity/commodities",
        "https://www.nasdaq.com"
    )
    all_articles.extend(articles1)
    await asyncio.sleep(1)

    articles2 = await fetch_from_source(
        "https://www.marketwatch.com/investing/commodities",
        "https://www.marketwatch.com"
    )
    all_articles.extend(articles2)
    await asyncio.sleep(1)

    articles3 = await fetch_from_source(
        "https://www.investing.com/commodities/",
        "https://www.investing.com"
    )
    all_articles.extend(articles3)

    seen = set()
    unique = []
    for a in all_articles:
        if a["title"] not in seen and a["title"] not in posted_news:
            seen.add(a["title"])
            unique.append(a)

    logger.info(f"Found {len(unique)} unique commodity articles")
    return unique[:20]


async def fetch_reuters_commodities():
    return await fetch_commodity_news_sources()


def format_commodity_news(news):
    emoji = news.get("emoji", "📰")
    title_vi = news.get("title_vi", "")
    analysis = news.get("analysis", "")
    article = news.get("article", {})
    link = article.get("link", "")

    lines = [
        f"{emoji} *{title_vi}*",
        f"",
        f"👉 {analysis}",
    ]
    if link:
        lines.append(f"\n🔗 [Đọc thêm]({link})")
    return "\n".join(lines)


async def filter_and_analyze_news(articles):
    if not articles:
        return []

    articles = articles[:15]
    titles_text = "\n".join([f"{i+1}. {a['title']}" for i, a in enumerate(articles)])

    try:
        prompt = f"""Ban la chuyen gia phan tich thi truong hang hoa phai sinh Viet Nam.

Danh sach {len(articles)} tin tuc hang hoa:
{titles_text}

Chon TOI DA 2 tin THUC SU quan trong voi hang hoa (thien tai, dia chinh tri, cung cau dot bien, chinh sach XNK lon).
Bo qua tin phan tich thong thuong, du bao gia, tin ky thuat.

Format tra ve CHINH XAC:
ARTICLE:[so]
TITLE_VI:[tieu de tieng Viet ngan gon]
ANALYSIS:[1-2 cau tac dong den hang hoa, co ma neu lien quan]
EMOJI:[1 emoji]
---

Neu khong co tin quan trong: NONE"""

        result = await asyncio.to_thread(call_gemini, prompt)

        if not result:
            logger.warning("Groq returned None, skipping")
            return []

        if "NONE" in result:
            logger.info("No important commodity news")
            return []

        selected = []
        blocks = result.split("---")
        for block in blocks:
            if "ARTICLE:" not in block:
                continue
            try:
                data = {}
                for line in block.strip().split("\n"):
                    if line.startswith("ARTICLE:"):
                        idx = int(line.replace("ARTICLE:", "").strip()) - 1
                        if 0 <= idx < len(articles):
                            data["article"] = articles[idx]
                    elif line.startswith("TITLE_VI:"):
                        data["title_vi"] = line.replace("TITLE_VI:", "").strip()
                    elif line.startswith("ANALYSIS:"):
                        data["analysis"] = line.replace("ANALYSIS:", "").strip()
                    elif line.startswith("EMOJI:"):
                        data["emoji"] = line.replace("EMOJI:", "").strip()
                if "title_vi" in data and "analysis" in data:
                    selected.append(data)
            except Exception as e:
                logger.error(f"Parse error: {e}")
                continue

        logger.info(f"Selected {len(selected)} articles")
        return selected

    except Exception as e:
        logger.error(f"Filter news error: {e}")
        return []


async def check_commodity_news(context=None):
    logger.info("Checking commodity news...")
    articles = await fetch_reuters_commodities()
    if not articles:
        return

    new_articles = [a for a in articles if a["title"] not in posted_news]
    if not new_articles:
        return

    selected = await filter_and_analyze_news(new_articles)
    if not selected:
        return

    bot = Bot(token=BOT_TOKEN)
    for news in selected:
        article = news.get("article", {})
        title = article.get("title", "")
        if title in posted_news:
            continue

        message = format_commodity_news(news)
        try:
            await bot.send_message(chat_id=CHANNEL_ID, text=message, parse_mode="Markdown")
            posted_news.add(title)
            logger.info(f"Posted commodity news: {news.get('title_vi', '')}")
            await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"Commodity news post error: {e}")


# ═══════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════

async def main():
    logger.info("News Bot dang chay...")

    # ---- AUTO TEST KHI KHỞI ĐỘNG ----
    logger.info("=== AUTO TEST ===")
    try:
        client = Groq(api_key=GEMINI_API_KEY)
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": "Viết 1 câu ngắn về giá vàng bằng tiếng Việt."}],
            max_tokens=100
        )
        analysis = response.choices[0].message.content.strip()
        logger.info(f"Groq OK: {analysis}")
        bot = Bot(token=BOT_TOKEN)
        await bot.send_message(chat_id=CHANNEL_ID, text=f"🤖 Bot online!\n\n👉 {analysis}")
        logger.info("=== AUTO TEST PASSED ✅ ===")
    except Exception as e:
        logger.error(f"=== AUTO TEST FAILED: {e} ===")
    # ---- HẾT TEST ----

    commodity_counter = 0

    while True:
        try:
            logger.info("--- Checking news sources ---")

            await check_forex_news()
            await asyncio.sleep(10)

            await check_eia_news()
            await asyncio.sleep(10)
            await check_usda_news()
            await asyncio.sleep(10)

            commodity_counter += 1
            if commodity_counter >= 3:
                await check_commodity_news()
                commodity_counter = 0
                await asyncio.sleep(10)

            logger.info("--- Done, sleeping 10 minutes ---")
            await asyncio.sleep(600)

        except Exception as e:
            logger.error(f"Main loop error: {e}")
            await asyncio.sleep(120)


async def test_bot():
    logger.info("=== RUNNING TEST ===")
    bot = Bot(token=BOT_TOKEN)

    # Test 1: Groq AI
    logger.info("Test 1: Groq AI...")
    try:
        client = Groq(api_key=GEMINI_API_KEY)
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": "Viết 1 câu ngắn về giá đậu tương bằng tiếng Việt."}],
            max_tokens=100
        )
        analysis = response.choices[0].message.content.strip()
        logger.info(f"Groq OK: {analysis[:50]}...")
    except Exception as e:
        logger.error(f"Groq FAILED: {e}")
        return

    # Test 2: Gửi tin test lên channel
    logger.info("Test 2: Sending test message to channel...")
    test_msg = (
        "⚡️ BREAKING NEWS | Core CPI m/m 🇺🇸\n"
        "\n"
        "📌 Actual: 0.4%  📈 Cao hơn dự báo\n"
        "📊 Dự báo: 0.3%\n"
        "📋 Trước đó: 0.2%\n"
        "\n"
        f"👉 {analysis}\n"
        "\n"
        "🤖 [TIN TEST - Xác nhận bot hoạt động]"
    )
    try:
        await bot.send_message(chat_id=CHANNEL_ID, text=test_msg)
        logger.info("✅ Test message sent successfully!")
    except Exception as e:
        logger.error(f"Telegram FAILED: {e}")
        return

    logger.info("=== TEST PASSED ✅ ===")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        asyncio.run(test_bot())
    else:
        asyncio.run(main())
