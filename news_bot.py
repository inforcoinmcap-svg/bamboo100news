import logging
import os
import json
import asyncio
import httpx
from datetime import datetime, time
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.ext import Application
import google.generativeai as genai

# ============================================================
BOT_TOKEN = os.environ.get("NEWS_BOT_TOKEN", "")
CHANNEL_ID = int(os.environ.get("NEWS_CHANNEL_ID", "0"))
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# ============================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# Lưu các tin đã đăng để tránh đăng lại
posted_events = set()

# Currencies và impact level cần theo dõi
WATCH_LIST = {
    "USD": ["high"],
    "CNY": ["high", "medium"],
}

CURRENCY_FLAGS = {
    "USD": "🇺🇸",
    "CNY": "🇨🇳",
    "EUR": "🇪🇺",
    "GBP": "🇬🇧",
    "JPY": "🇯🇵",
    "AUD": "🇦🇺",
}

IMPACT_ICONS = {
    "high": "🔴",
    "medium": "🟡",
    "low": "⚪",
}


def get_vn_time():
    return datetime.now(VN_TZ)


async def fetch_forexfactory():
    """Scrape lịch kinh tế ForexFactory hôm nay"""
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
        logger.error(f"Error fetching ForexFactory: {e}")
        return None


def parse_calendar(html):
    """Parse HTML ForexFactory lấy các sự kiện"""
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    events = []

    calendar_table = soup.find("table", class_="calendar__table")
    if not calendar_table:
        logger.warning("Calendar table not found")
        return []

    rows = calendar_table.find_all("tr", class_="calendar__row")
    current_date = ""

    for row in rows:
        try:
            # Lấy ngày nếu có
            date_cell = row.find("td", class_="calendar__date")
            if date_cell and date_cell.text.strip():
                current_date = date_cell.text.strip()

            # Lấy giờ
            time_cell = row.find("td", class_="calendar__time")
            if not time_cell:
                continue
            event_time = time_cell.text.strip()

            # Lấy currency
            currency_cell = row.find("td", class_="calendar__currency")
            if not currency_cell:
                continue
            currency = currency_cell.text.strip()

            # Chỉ lấy USD và CNY
            if currency not in WATCH_LIST:
                continue

            # Lấy impact
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

            # Kiểm tra watch list
            if impact not in WATCH_LIST.get(currency, []):
                continue

            # Lấy tên sự kiện
            event_cell = row.find("td", class_="calendar__event")
            if not event_cell:
                continue
            event_name = event_cell.text.strip()

            # Lấy actual, forecast, previous
            actual_cell = row.find("td", class_="calendar__actual")
            forecast_cell = row.find("td", class_="calendar__forecast")
            previous_cell = row.find("td", class_="calendar__previous")

            actual = actual_cell.text.strip() if actual_cell else ""
            forecast = forecast_cell.text.strip() if forecast_cell else ""
            previous = previous_cell.text.strip() if previous_cell else ""

            # Tạo event ID duy nhất
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
            logger.error(f"Error parsing row: {e}")
            continue

    return events


async def analyze_with_claude(event):
    """Gọi Gemini AI để viết phân tích tin"""
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-1.5-flash")

        flag = CURRENCY_FLAGS.get(event["currency"], "")
        actual = event["actual"]
        forecast = event["forecast"]
        previous = event["previous"]

        # So sánh actual vs forecast
        comparison = ""
        try:
            act_val = float(actual.replace("%", "").replace("K", "000").replace("M", "000000"))
            fct_val = float(forecast.replace("%", "").replace("K", "000").replace("M", "000000"))
            if act_val > fct_val:
                comparison = "cao hơn dự báo"
            elif act_val < fct_val:
                comparison = "thấp hơn dự báo"
            else:
                comparison = "đúng dự báo"
        except:
            comparison = "so với dự báo"

        prompt = f"""Bạn là chuyên gia phân tích thị trường tài chính và hàng hoá phái sinh.

Vừa có tin kinh tế quan trọng được công bố:
- Tên tin: {event['name']}
- Đồng tiền: {event['currency']} {flag}
- Actual: {actual} ({comparison})
- Forecast: {forecast}
- Previous: {previous}

Hãy viết 2-3 câu phân tích ngắn gọn về:
1. Ý nghĩa của số liệu này so với kỳ vọng
2. Tác động ngắn hạn đến thị trường hàng hoá (nông sản, kim loại, năng lượng)

Yêu cầu:
- Viết bằng tiếng Việt
- Tự nhiên, đa dạng văn phong, không dùng template cứng
- Ngắn gọn, súc tích, đi thẳng vào tác động
- Không bắt đầu bằng "Phân tích:" hay "Nhận xét:"
- Chỉ trả về đoạn phân tích, không có gì thêm"""

        response = await asyncio.to_thread(model.generate_content, prompt)
        return response.text.strip()

    except Exception as e:
        logger.error(f"Gemini API error: {e}")
        return None


def format_news_message(event, analysis):
    """Format tin nhắn đăng lên Telegram"""
    flag = CURRENCY_FLAGS.get(event["currency"], "")
    impact_icon = IMPACT_ICONS.get(event["impact"], "⚪")
    now = get_vn_time()

    # So sánh actual vs forecast để highlight
    actual = event["actual"]
    forecast = event["forecast"]
    previous = event["previous"]

    try:
        act_val = float(actual.replace("%", "").replace("K", "000").replace("M", "000000").replace(",", ""))
        fct_val = float(forecast.replace("%", "").replace("K", "000").replace("M", "000000").replace(",", ""))
        if act_val > fct_val:
            result_icon = "📈"
            result_text = "Cao hơn dự báo"
        elif act_val < fct_val:
            result_icon = "📉"
            result_text = "Thấp hơn dự báo"
        else:
            result_icon = "➡️"
            result_text = "Đúng dự báo"
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
        lines.append(f"")

    lines.append(f"🕐 {now.strftime('%H:%M GMT+7 | %d/%m/%Y')}")

    return "\n".join(lines)


async def check_and_post_news(context=None):
    """Kiểm tra và đăng tin mới"""
    logger.info("Checking ForexFactory for new events...")

    html = await fetch_forexfactory()
    if not html:
        return

    events = parse_calendar(html)
    logger.info(f"Found {len(events)} relevant events")

    bot = Bot(token=BOT_TOKEN)

    for event in events:
        # Chỉ đăng tin đã có actual (tin đã ra)
        if not event["actual"]:
            continue

        # Tránh đăng lại
        if event["id"] in posted_events:
            continue

        logger.info(f"New event: {event['name']} | {event['currency']} | Actual: {event['actual']}")

        # Gọi Claude phân tích
        analysis = await analyze_with_claude(event)

        # Format tin nhắn
        message = format_news_message(event, analysis)

        # Đăng lên channel
        try:
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=message,
                parse_mode=None
            )
            posted_events.add(event["id"])
            logger.info(f"Posted: {event['name']}")
            await asyncio.sleep(2)  # Tránh spam
        except Exception as e:
            logger.error(f"Error posting to channel: {e}")


async def main():
    """Chạy bot"""
    logger.info("🚀 News Bot đang chạy...")

    app = Application.builder().token(BOT_TOKEN).build()

    # Check tin mỗi 2 phút
    app.job_queue.run_repeating(
        check_and_post_news,
        interval=120,
        first=10,
    )

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    logger.info("Bot started, checking news every 2 minutes...")

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
