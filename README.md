# ultitec-automation

Jason 的排程任務共用程式。程式負責抓資料和計算，Claude 負責判讀、撰寫簡報和推播。

## prices.py — 收盤取價與損益

```bash
# 1) Claude 先把 Notion「Holdings Master」整理成 holdings.json（格式見下方）
# 2) 執行
python3 prices.py --market us --holdings holdings.json --json-out us.json
python3 prices.py --market tw --holdings holdings.json --json-out tw.json
# 只取價（RCN 接股風險儀表板）
python3 prices.py --market us --symbols MU,INTC,AMD --prices-out prices.json
```

- 資料來源：台股以證交所官方資料為主、Yahoo 備援；美股用 Yahoo Finance chart API
- 交易日曆檢查：任一標的缺當日或前一交易日資料時自動改抓另一來源，仍缺則警告
- 自動判定已收盤的最近交易日 T（美股以美東 16:15、台股以台北 14:30 為準）
- 合理性檢查：缺 T 日收盤、00631L 與 0050×2 偏差、異常漲跌幅、匯率範圍
- 必須提供 holdings.json，否則程式結束並回報錯誤
- **warnings 必須寫進簡報與推播**

holdings.json：

```json
{"us": [{"symbol": "NVDA", "shares": 100, "cost": 150.0}],
 "tw": [{"symbol": "2330", "shares": 1000, "cost": 1000.0}],
 "fallback": "2026/10/29"}
```

`fallback` 選填：Notion 讀取失敗、改用排程指令中的備用清單時填入日期，程式會產生警告。

本 repo 為公開，**不放任何持股、帳號、密碼等個人資料**；持股每次由排程從 Notion 讀取後寫入 holdings.json（已列入 .gitignore）。
