#!/usr/bin/env python3
"""
机票价格监控程序 v3
航线：成都双流(CTU) → 上海虹桥(SHA)
日期：2026-05-10
筛选条件：
  - 价格低于 700 元
  - 出发时间：9:00-16:00（大机型时间筛选）
  - 机型：大机型（宽体机）
"""

import asyncio
import json
import os
import sys
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, List

try:
    from playwright.async_api import async_playwright, Response
except ImportError:
    print("❌ playwright 未安装")
    sys.exit(1)

# ============ 配置 ============
CONFIG = {
    "dep_code": "CTU",
    "arr_code": "SHA",
    "dep_city_name": "成都",
    "arr_city_name": "上海",
    "date": "2026-05-10",
    "price_threshold": 700,         # 价格阈值（元）
    "dep_time_start": "09:00",      # 最早出发时间
    "dep_time_end": "16:00",        # 最晚出发时间
    "large_aircraft_only": True,    # 只看大机型
    "headless": True,
}

# 大机型代码列表（宽体机）
LARGE_AIRCRAFT_CODES = {
    'A300', 'A310', 'A330', 'A340', 'A350', 'A380',
    'B747', 'B767', 'B777', 'B787',
    'IL96', 'AN224',
}

FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
PRICE_LOG_FILE = Path(__file__).parent / "price_log.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def load_price_log() -> dict:
    if PRICE_LOG_FILE.exists():
        try:
            return json.loads(PRICE_LOG_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_price_log(log_data: dict):
    PRICE_LOG_FILE.write_text(json.dumps(log_data, ensure_ascii=False, indent=2))


def send_feishu_message(message: str) -> bool:
    if not FEISHU_WEBHOOK:
        logger.warning("⚠️ 未配置 FEISHU_WEBHOOK 环境变量")
        return False
    
    import urllib.request
    
    payload = {"msg_type": "text", "content": {"text": message}}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        FEISHU_WEBHOOK,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("code") == 0:
                logger.info("✅ 飞书通知已发送")
                return True
            logger.error(f"❌ 飞书返回错误: {result}")
            return False
    except Exception as e:
        logger.error(f"❌ 飞书通知失败: {e}")
        return False


def filter_by_departure_time(flights: List[dict], time_start: str, time_end: str) -> List[dict]:
    """根据出发时间筛选航班"""
    if not flights:
        return []
    
    start_h, start_m = map(int, time_start.split(':'))
    end_h, end_m = map(int, time_end.split(':'))
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m
    
    filtered = []
    for flight in flights:
        dep_time = flight.get('depTime', '')
        if not dep_time:
            continue
        
        dep_clean = dep_time.replace(':', '')
        if len(dep_clean) >= 4:
            dep_h = int(dep_clean[:2])
            dep_m = int(dep_clean[2:4])
            dep_minutes = dep_h * 60 + dep_m
            
            if start_minutes <= dep_minutes <= end_minutes:
                filtered.append(flight)
    
    return filtered


def filter_by_aircraft(flights: List[dict], large_only: bool) -> List[dict]:
    """根据机型筛选 - 大机型（宽体机）"""
    if not flights or not large_only:
        return flights
    
    filtered = []
    for flight in flights:
        plane_type = flight.get('planeType', '') or flight.get('aircraft', '') or ''
        is_large = any(code in plane_type.upper() for code in LARGE_AIRCRAFT_CODES)
        if is_large:
            filtered.append(flight)
    
    return filtered


def parse_price_calendar(text: str) -> dict:
    """从价格日历 API 响应中解析目标日期的价格"""
    try:
        data = json.loads(text)
        price_list = data.get('priceList', [])
        target_date = CONFIG['date']  # "2026-05-10"
        
        for item in price_list:
            depart_date = item.get('departDate', '')
            # departDate 格式: /Date(1776787200000+0800)/
            if not depart_date:
                continue
            
            # 提取时间戳
            match = re.search(r'\d+', str(depart_date))
            if not match:
                continue
            
            ts_ms = int(match.group())
            ts_sec = ts_ms / 1000
            
            # 转换为日期字符串
            import datetime
            dt = datetime.datetime.fromtimestamp(
                ts_sec, 
                tz=datetime.timezone(datetime.timedelta(hours=8))
            )
            date_str = dt.strftime('%Y-%m-%d')
            
            if date_str == target_date:
                return {
                    'date': date_str,
                    'transportPrice': item.get('price', 0),
                    'totalPrice': item.get('totalPrice', 0),
                    'flightNo': item.get('flightNo', ''),
                    'airLine': item.get('airLine', ''),
                }
    except Exception as e:
        logger.error(f"解析价格日历失败: {e}")
    
    return {}


async def scrape_flights() -> tuple:
    """从携程抓取航班数据"""
    url = (
        f"https://flights.ctrip.com/international/search/oneway-"
        f"{CONFIG['dep_code'].lower()}-{CONFIG['arr_code'].lower()}"
        f"?depdate={CONFIG['date']}&cabin=y&adult=1&child=0"
    )
    
    logger.info(f"🔍 正在查询: {CONFIG['dep_city_name']} → {CONFIG['arr_city_name']} ({CONFIG['date']})")
    
    flight_details = []
    price_calendar_data = []
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=CONFIG["headless"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        async def handle_response(response: Response):
            try:
                url_lower = response.url.lower()
                if not any(k in url_lower for k in ["search", "flight", "batch"]):
                    return
                if "ctrip" not in url_lower and "tripcdn" not in url_lower:
                    return
                
                body = await response.text()
                if not body or len(body) < 500:
                    return
                
                if any(k in body for k in ['"flightNo"']) and any(k in body for k in ['"depTime"', '"planeType"']):
                    # 真正的航班详情同时包含 flightNo（带值）和 depTime/planeType
                    if '"flightNo":""' not in body and '"flightNo": ""' not in body:
                        flight_details.append(body)
                    elif 'FlightIntlAndInlandLowestPrice' in response.url:
                        price_calendar_data.append(body)
                elif 'FlightIntlAndInlandLowestPrice' in response.url:
                    price_calendar_data.append(body)
            except Exception:
                pass

        page.on("response", handle_response)
        
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(8)
            
            # 从页面 DOM 提取
            try:
                await page.wait_for_selector(
                    "[class*='flight'], [class*='itinerary'], [class*='cabin']",
                    timeout=15000
                )
            except Exception:
                pass
            
            await asyncio.sleep(3)
            
        except Exception as e:
            logger.error(f"❌ 页面加载异常: {e}")
        finally:
            await browser.close()
    
    # 解析价格日历
    target_price = {}
    for data in price_calendar_data:
        price_info = parse_price_calendar(data)
        if price_info:
            target_price = price_info
            break
    
    # 解析航班详情
    parsed_flights = []
    for detail in flight_details:
        flights = parse_flight_details(detail)
        parsed_flights.extend(flights)
    
    # 去重
    seen = set()
    unique_flights = []
    for f in parsed_flights:
        key = f.get('flightNo', '') + f.get('depTime', '')
        if key and key not in seen:
            seen.add(key)
            unique_flights.append(f)
    
    return unique_flights, target_price


def parse_flight_details(text: str) -> list:
    """解析航班详情文本"""
    flights = []
    if not text:
        return flights
    
    try:
        flight_nos = re.findall(r'"flightNo"\s*:\s*"([^"]+)"', text)
        dep_times = re.findall(r'"depTime"\s*:\s*"([^"]+)"', text) or re.findall(r'"departTime"\s*:\s*"([^"]+)"', text)
        arr_times = re.findall(r'"arrTime"\s*:\s*"([^"]+)"', text) or re.findall(r'"arrivalTime"\s*:\s*"([^"]+)"', text)
        plane_types = re.findall(r'"planeType"\s*:\s*"([^"]+)"', text) or re.findall(r'"aircraft"\s*:\s*"([^"]+)"', text)
        prices = re.findall(r'"price"\s*:\s*(\d+)', text)
        total_prices = re.findall(r'"totalPrice"\s*:\s*(\d+)', text)
        
        for i in range(min(len(flight_nos), 30)):
            flight = {
                "flightNo": flight_nos[i] if i < len(flight_nos) else "",
                "depTime": dep_times[i] if i < len(dep_times) else "",
                "arrTime": arr_times[i] if i < len(arr_times) else "",
                "planeType": plane_types[i] if i < len(plane_types) else "",
                "price": int(prices[i]) if i < len(prices) else 0,
                "totalPrice": int(total_prices[i]) if i < len(total_prices) else 0,
            }
            flights.append(flight)
        
        if flights:
            logger.info(f"✈️ 解析到 {len(flights)} 个航班详情")
    
    except Exception as e:
        logger.error(f"❌ 解析航班详情失败: {e}")
    
    return flights


async def main() -> dict:
    logger.info("=" * 50)
    logger.info("✈️ 机票价格监控 v3 启动")
    logger.info(f"📍 {CONFIG['dep_city_name']} → {CONFIG['arr_city_name']} | {CONFIG['date']}")
    logger.info(f"💰 价格阈值: {CONFIG['price_threshold']}元")
    logger.info(f"🕙 出发时间: {CONFIG['dep_time_start']}-{CONFIG['dep_time_end']}")
    if CONFIG['large_aircraft_only']:
        logger.info(f"🛫 机型: 大机型（宽体机）")
    logger.info("=" * 50)

    # 抓取航班数据
    flights, price_info = await scrape_flights()
    
    logger.info(f"📊 价格日历信息: {price_info}")
    logger.info(f"📊 详细航班数量: {len(flights)}")
    
    # 应用筛选
    filtered_flights = flights
    if CONFIG['dep_time_start'] and CONFIG['dep_time_end']:
        before = len(filtered_flights)
        filtered_flights = filter_by_departure_time(filtered_flights, CONFIG['dep_time_start'], CONFIG['dep_time_end'])
        logger.info(f"⏰ 时间筛选: {before} → {len(filtered_flights)}")
    
    if CONFIG['large_aircraft_only']:
        before = len(filtered_flights)
        filtered_flights = filter_by_aircraft(filtered_flights, True)
        logger.info(f"🛫 机型筛选: {before} → {len(filtered_flights)}")
    
    # 确定价格
    cheapest = None
    has_cheap = False
    cheap_prices = []
    
    if filtered_flights:
        # 有详细航班数据
        cheap_flights = [f for f in filtered_flights if f.get('price', 0) <= CONFIG['price_threshold']]
        if cheap_flights:
            cheapest = min(f['price'] for f in cheap_flights)
            cheap_prices = sorted(set(f['price'] for f in cheap_flights))
            has_cheap = True
        else:
            cheapest = min(f['price'] for f in filtered_flights)
    elif price_info:
        # 使用价格日历
        tp = price_info.get('transportPrice', 0)
        total_p = price_info.get('totalPrice', 0)
        if tp > 0:
            cheapest = tp
            if tp <= CONFIG['price_threshold']:
                has_cheap = True
                cheap_prices = [tp]
            logger.info(f"💰 价格日历: transportPrice={tp}, totalPrice={total_p}")
    
    # 构建结果
    result = {
        "has_cheap": has_cheap,
        "cheapest": cheapest,
        "cheap_prices": cheap_prices,
        "flights": filtered_flights[:10],
        "price_info": price_info,
    }
    
    # 保存记录
    price_log = load_price_log()
    today_key = datetime.now().strftime("%Y-%m-%d")
    if today_key not in price_log:
        price_log[today_key] = {}
    price_log[today_key][CONFIG["date"]] = {
        "cheapest": cheapest,
        "cheap_prices": cheap_prices,
        "price_info": price_info,
        "flights_count": len(filtered_flights),
        "checked_at": datetime.now().isoformat(),
    }
    save_price_log(price_log)
    
    # 生成通知消息
    if has_cheap:
        if filtered_flights:
            flight_list = "\n".join([
                f"✈️ {f['flightNo']} {f['depTime']} | {f['planeType']} | ¥{f['price']}"
                for f in filtered_flights[:5] if f.get('price', 0) <= CONFIG['price_threshold']
            ])
            message = (
                f"🎉 好消息！找到符合条件的机票！\n\n"
                f"📍 航线: {CONFIG['dep_city_name']} → {CONFIG['arr_city_name']}\n"
                f"📅 日期: {CONFIG['date']}\n"
                f"🕙 时间: {CONFIG['dep_time_start']}-{CONFIG['dep_time_end']}\n"
                f"🛫 机型: 大机型\n\n"
                f"{flight_list}\n\n"
                f"最低价: ¥{cheap_prices[0]}\n\n"
                f"🔗 https://flights.ctrip.com"
            )
        else:
            message = (
                f"🎉 好消息！{CONFIG['date']} 机票低于 {CONFIG['price_threshold']}元！\n\n"
                f"📍 航线: {CONFIG['dep_city_name']} → {CONFIG['arr_city_name']}\n"
                f"📅 日期: {CONFIG['date']}\n"
                f"💰 票价: ¥{cheap_prices[0]}（机建税前）\n\n"
                f"⚠️ 详细航班时间/机型暂不可用\n"
                f"💡 需手动确认航班时间是否在 {CONFIG['dep_time_start']}-{CONFIG['dep_time_end']} 内\n\n"
                f"🔗 https://flights.ctrip.com"
            )
    elif cheapest:
        message = (
            f"😕 当前暂无符合条件的特价机票\n\n"
            f"📍 航线: {CONFIG['dep_city_name']} → {CONFIG['arr_city_name']}\n"
            f"📅 日期: {CONFIG['date']}\n"
            f"💰 最低价: ¥{cheapest}\n"
            f"🕙 时间: {CONFIG['dep_time_start']}-{CONFIG['dep_time_end']}\n\n"
            f"🔗 https://flights.ctrip.com"
        )
    else:
        message = (
            f"⚠️ 未能获取到航班数据\n\n"
            f"📍 航线: {CONFIG['dep_city_name']} → {CONFIG['arr_city_name']}\n"
            f"📅 日期: {CONFIG['date']}\n"
            f"请稍后手动查询"
        )
    
    print("\n" + "=" * 50)
    print(message)
    print("=" * 50 + "\n")
    
    # 发送通知
    if has_cheap and FEISHU_WEBHOOK:
        send_feishu_message(message)
    elif has_cheap:
        logger.info("💡 配置 FEISHU_WEBHOOK 环境变量即可发送飞书通知")
    
    return result


if __name__ == "__main__":
    result = asyncio.run(main())
    sys.exit(0 if result.get("cheapest") else 1)
