# ✈️ 机票价格监控

成都双流 → 上海虹桥 | 2026年5月10日 | 低于700元通知

## 功能

- 🎯 监控指定航线指定日期的机票价格
- 🔔 价格低于阈值时自动发送飞书通知
- 📊 记录历史价格，方便查看走势
- ⏰ 可设置定时任务每小时自动检查

## 使用方法

```bash
# 1. 克隆仓库
git clone https://github.com/pszathpan-cmd/flight-monitor.git
cd flight-monitor

# 2. 安装依赖
pip install playwright
python -m playwright install chromium

# 3. 设置飞书机器人（可选）
export FEISHU_WEBHOOK="你的飞书Webhook地址"

# 4. 运行
python3 flight_monitor.py
```

## 配置

编辑 `flight_monitor.py` 中的 `CONFIG` 字典：

```python
CONFIG = {
    "dep_code": "CTU",        # 出发机场
    "arr_code": "SHA",        # 到达机场
    "dep_city_name": "成都",
    "arr_city_name": "上海",
    "date": "2026-05-10",    # 出发日期
    "price_threshold": 700,   # 价格阈值
}
```

## 部署定时任务

```bash
# 每小时检查一次
0 * * * * cd /root/.openclaw/workspace/flight_monitor && python3 flight_monitor.py
```

## 航线查询

- 携程：https://flights.ctrip.com
- 去哪儿：https://www.qunar.com
