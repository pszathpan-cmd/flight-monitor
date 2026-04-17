#!/usr/bin/env python3
"""
机票价格监控程序 v2
航线：成都双流(CTU) → 上海虹桥(SHA)
日期：2026-05-10
阈值：低于700元通知
"""

import asyncio
import json
import os
import sys
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

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
    "price_threshold": 700,
    "headless": True,
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


def extract_prices_from_text(text: str) -> list:
    """从文本中提取价格"""
    prices = []
    # 匹配各种价格格式
    patterns = [
        r'"price"\s*:\s*(\d{3,4})',
        r'"adultPrice"\s*:\s*(\d{3,4})',
        r'"cabinPrice"\s*:\s*(\d{3,4})',
        r'(\d{4})元',  # 4位数价格
        r'[\u4e00-\u9fa5]?\s*(\d{3})\s*元',  # 3位数价格
    ]
    for pattern in patterns:
        matches = re.findall(pattern, text)
        for m in matches:
            price = int(m)
            if 100 < price < 5000:  # 合理价格范围
                prices.append(price)
    return sorted(set(prices))


async def main() -> dict:
    logger.info("=" * 50)
    logger.info("✈️ 机票价格监控启动")
    logger.info(f"📍 {CONFIG['dep_city_name']} → {CONFIG['arr_city_name']} | {CONFIG['date']}")
    logger.info(f"💰 阈值: {CONFIG['price_threshold']}元")
    logger.info("=" * 50)

    url = (
        f"https://flights.ctrip.com/international/search/oneway-"
        f"{CONFIG['dep_code'].lower()}-{CONFIG['arr_code'].lower()}"
        f"?depdate={CONFIG['date']}&cabin=y&adult=1&child=0"
    )

    flight_api_data = []
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=CONFIG["headless"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        # 拦截航班数据API
        async def handle_response(response: Response):
            url_lower = response.url.lower()
            # 捕获包含航班数据的API响应
            if any(k in url_lower for k in ["search", "flight", "batch", "schedule"]):
                if "ctrip" in url_lower or "tripcdn" in url_lower:
                    try:
                        body = await response.text()
                        if body and len(body) > 500:
                            prices = extract_prices_from_text(body)
                            if prices:
                                logger.info(f"📦 API数据 ({response.url[:60]}...): 发现 {len(prices)} 个价格")
                                flight_api_data.append({
                                    "url": response.url,
                                    "body": body,
                                    "prices": prices,
                                })
                    except Exception as e:
                        pass

        page.on("response", handle_response)

        try:
            logger.info(f"🌐 正在加载页面...")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            
            # 等待页面加载完成
            await asyncio.sleep(8)
            
            # 尝试从页面state获取数据
            logger.info("🔍 等待航班数据加载...")
            
            # 等待价格元素出现（携程的价格通常在特定class里）
            try:
                await page.wait_for_selector(
                    ".flight-list, .search-result-list, [class*='price'], [class*='flight']",
                    timeout=15000
                )
                logger.info("✅ 航班列表已加载")
            except Exception:
                logger.warning("⚠️ 未检测到航班列表元素，继续尝试其他方式...")
            
            await asyncio.sleep(3)
            
            # 从页面DOM提取价格
            dom_prices = await page.evaluate("""
                () => {
                    // 查找所有价格相关元素
                    const priceEls = document.querySelectorAll('[class*="price"], [class*="Price"]');
                    const prices = [];
                    priceEls.forEach(el => {
                        const text = el.innerText || el.textContent;
                        const match = text.match(/(\\d{3,4})/);
                        if (match) prices.push(parseInt(match[1]));
                    });
                    
                    // 也尝试从script标签提取
                    const scripts = document.querySelectorAll('script');
                    let dataStr = '';
                    scripts.forEach(s => {
                        const t = s.innerText;
                        if (t.includes('price') && t.includes('flight')) {
                            dataStr += t;
                        }
                    });
                    
                    // 提取所有数字价格
                    const allNums = dataStr.match(/"price"\\s*:\\s*(\\d{3,4})/g) || [];
                    allNums.forEach(m => {
                        const n = parseInt(m.match(/\\d+/)[0]);
                        if (n > 100 && n < 5000) prices.push(n);
                    });
                    
                    return [...new Set(prices)].sort((a,b) => a-b);
                }
            """)
            
            if dom_prices:
                logger.info(f"💰 DOM提取到价格: {dom_prices[:15]}")
                for p in flight_api_data:
                    p["prices"] = list(set(p["prices"] + dom_prices))

        except Exception as e:
            logger.error(f"❌ 页面加载异常: {e}")
        finally:
            await browser.close()

    # 汇总所有发现的价格
    all_prices = []
    for api_data in flight_api_data:
        all_prices.extend(api_data["prices"])
    
    all_prices = sorted(set([p for p in all_prices if 100 < p < 5000]))
    logger.info(f"📊 共发现 {len(all_prices)} 个价格: {all_prices[:20]}")

    # 价格检查
    cheapest = all_prices[0] if all_prices else None
    has_cheap = any(p <= CONFIG["price_threshold"] for p in all_prices)
    cheap_prices = [p for p in all_prices if p <= CONFIG["price_threshold"]]
    
    result = {
        "has_cheap": has_cheap,
        "cheapest": cheapest,
        "cheap_prices": cheap_prices,
        "all_prices": all_prices,
    }

    # 保存记录
    price_log = load_price_log()
    today_key = datetime.now().strftime("%Y-%m-%d")
    if today_key not in price_log:
        price_log[today_key] = {}
    price_log[today_key][CONFIG["date"]] = {
        "cheapest": cheapest,
        "all_prices": all_prices[:50],
        "checked_at": datetime.now().isoformat(),
    }
    save_price_log(price_log)

    # 生成通知消息
    if has_cheap:
        flight_list = "\n".join([f"💺 {p}元" for p in cheap_prices[:5]])
        message = (
            f"🎉 好消息！找到低于 {CONFIG['price_threshold']}元的机票！\n\n"
            f"📍 航线: {CONFIG['dep_city_name']} → {CONFIG['arr_city_name']}\n"
            f"📅 日期: {CONFIG['date']}\n\n"
            f"{flight_list}\n\n"
            f"最低价: {cheap_prices[0]}元\n\n"
            f"🔗 https://flights.ctrip.com"
        )
    elif cheapest:
        message = (
            f"😕 当前暂无低于 {CONFIG['price_threshold']}元的机票\n\n"
            f"📍 航线: {CONFIG['dep_city_name']} → {CONFIG['arr_city_name']}\n"
            f"📅 日期: {CONFIG['date']}\n"
            f"💰 最低价: {cheapest}元\n\n"
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
    sys.exit(0 if result["cheapest"] else 1)
