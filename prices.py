#!/usr/bin/env python3
"""
prices.py — Jason 投資組合收盤取價與損益計算（ULTITEC automation）

用法：
  python3 prices.py --market us --holdings holdings.json
  python3 prices.py --market tw --holdings holdings.json
  python3 prices.py --market us --symbols MU,INTC,AMD --prices-out prices.json   # 只取價（RCN 儀表板用）

holdings.json 格式（由 Claude 從 Notion「Holdings Master」整理後寫入；Notion 失敗時改寫入排程指令中的備用清單，並加 "fallback": "日期"）：
  {"us": [{"symbol": "NVDA", "shares": 100, "cost": 150.0}, ...],
   "tw": [{"symbol": "2330", "shares": 1000, "cost": 1000.0}, ...],
   "fallback": "2026/10/29"}   # 選填：表示使用備用舊資料

輸出：
  stdout 印出 Markdown 表格與檢查訊息；--json-out 另存完整結果 JSON。
  資料有疑慮時在 "warnings" 列出，Claude 必須把 warnings 寫進簡報與推播。
"""
import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UA = {"User-Agent": "Mozilla/5.0"}
ET = ZoneInfo("America/New_York")
TPE = ZoneInfo("Asia/Taipei")


INDICES = {
    "us": [("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq"), ("^DJI", "Dow"), ("^SOX", "費城半導體")],
    "tw": [("^TWII", "加權指數")],
}
MARKET = {
    "us": {"tz": ET, "close_after": (16, 15)},   # 美東 16:15 後視為已收盤
    "tw": {"tz": TPE, "close_after": (14, 30)},  # 台北 14:30 後視為已收盤
}


def yahoo_symbol(sym, market):
    if market == "tw" and not sym.startswith("^") and "." not in sym:
        return sym + ".TW"
    return sym


def fetch_chart(ysym, rng="10d", retries=2):
    last = None
    for host in ("query1", "query2"):
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ysym)}?range={rng}&interval=1d"
        for i in range(retries):
            try:
                req = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(req, timeout=20) as r:
                    data = json.load(r)
                res = data["chart"]["result"][0]
                off = res["meta"].get("gmtoffset", 0)
                q = res["indicators"]["quote"][0]
                dedup = {}
                for ts, c, h in zip(res.get("timestamp", []), q.get("close", []), q.get("high", [])):
                    if c is None:
                        continue
                    d = datetime.fromtimestamp(ts + off, tz=timezone.utc).date()
                    dedup[d] = {"date": d, "close": round(c, 4), "high": round(h, 4) if h else None}
                if dedup:
                    return [dedup[k] for k in sorted(dedup)], "yahoo"
            except Exception as e:  # noqa
                last = e
                time.sleep(2)
    raise RuntimeError(f"{ysym} Yahoo 取價失敗：{last}")


def _twse_get(path, params):
    url = "https://www.twse.com.tw/rwd/zh/afterTrading/" + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as r:
        j = json.load(r)
    if j.get("stat") != "OK":
        raise RuntimeError(j.get("stat"))
    return j


def _roc(s):
    y, m, d = s.split("/")
    return datetime(int(y) + 1911, int(m), int(d)).date()


def _num(s):
    s = str(s).replace(",", "").strip()
    return float(s) if s not in ("", "--", "---") else None


def fetch_twse(sym):
    """證交所備援：個股/ETF 用 STOCK_DAY，加權指數用 FMTQIK；抓本月與上月。"""
    today = datetime.now(TPE).date()
    months = [today.replace(day=1), (today.replace(day=1) - timedelta(days=1)).replace(day=1)]
    rows = {}
    for mth in months:
        ds = mth.strftime("%Y%m%d")
        if sym == "^TWII":
            j = _twse_get("FMTQIK", {"date": ds, "response": "json"})
            for r in j.get("data", []):
                c = _num(r[4])
                if c:
                    rows[_roc(r[0])] = {"date": _roc(r[0]), "close": c, "high": None}
        else:
            j = _twse_get("STOCK_DAY", {"date": ds, "stockNo": sym, "response": "json"})
            for r in j.get("data", []):
                c = _num(r[6])
                if c:
                    rows[_roc(r[0])] = {"date": _roc(r[0]), "close": c, "high": _num(r[4])}
        time.sleep(1)
    if not rows:
        raise RuntimeError(f"{sym} 證交所無資料")
    return [rows[k] for k in sorted(rows)], "twse"


def get_rows(key, ysym, mk):
    """台股：證交所為主、Yahoo 備援；美股：Yahoo。"""
    if mk == "tw" and key != "TWD=X":
        try:
            return fetch_twse(key)
        except Exception:
            return fetch_chart(ysym)
    return fetch_chart(ysym)


def alt_rows(key, ysym, mk):
    """主來源缺日時改抓另一來源。"""
    if mk == "tw" and key != "TWD=X":
        return fetch_chart(ysym)  # 主來源是證交所 → 改 Yahoo
    if mk == "tw":
        return None
    try:  # 美股：改用另一主機重抓較長區間
        return fetch_chart(ysym, rng="1mo")
    except Exception:
        return None


def target_date(market, sample_rows):
    """T = 已收盤的最近交易日（以實際有資料的日期為準）。"""
    cfg = MARKET[market]
    now = datetime.now(cfg["tz"])
    h, m = cfg["close_after"]
    closed_today = (now.hour, now.minute) >= (h, m)
    candidates = [r["date"] for r in sample_rows
                  if r["date"] < now.date() or (r["date"] == now.date() and closed_today)]
    if not candidates:
        raise RuntimeError("找不到已收盤交易日")
    return max(candidates), now


def pick(rows, T, P=None):
    cur = next((r for r in rows if r["date"] == T), None)
    if P is not None:
        return cur, next((r for r in rows if r["date"] == P), None)
    prev = [r for r in rows if r["date"] < T]
    return cur, (prev[-1] if prev else None)


def fmt(n, d=0):
    if n is None:
        return "—"
    return f"{n:,.{d}f}"


def pct(n):
    return "—" if n is None else f"{n:+.2f}%"


def main():
    import urllib.parse  # noqa
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["us", "tw"], required=True)
    ap.add_argument("--holdings", help="holdings.json 路徑")
    ap.add_argument("--symbols", help="只取價模式：逗號分隔代號")
    ap.add_argument("--prices-out", help="只取價模式輸出檔")
    ap.add_argument("--json-out", help="完整結果 JSON 輸出檔")
    a = ap.parse_args()
    mk = a.market
    warnings = []

    # ---- 持股 ----
    using_fallback = False
    if a.symbols:
        holdings = [{"symbol": s.strip().upper(), "shares": 0, "cost": 0} for s in a.symbols.split(",") if s.strip()]
    else:
        holdings = None
        if a.holdings:
            try:
                with open(a.holdings, encoding="utf-8") as f:
                    hj = json.load(f)
                holdings = hj.get(mk)
                if hj.get("fallback"):
                    using_fallback = True
                    warnings.append(f"⚠️ Notion持股主檔讀取失敗，本次使用備用舊資料（{hj['fallback']}）")
            except Exception as e:
                sys.exit(f"錯誤：holdings 檔讀取失敗（{e}）")
        if not holdings:
            sys.exit("錯誤：需要 --holdings holdings.json（含 %s 持股）" % mk)

    # ---- 取價 ----
    charts, errors = {}, {}
    syms = [h["symbol"] for h in holdings]
    idx = [] if a.symbols else INDICES[mk]
    all_y = [(s, yahoo_symbol(s, mk)) for s in syms] + [(s, s) for s, _ in idx]
    if mk == "us" and not a.symbols:
        all_y.append(("TWD=X", "TWD=X"))
    for key, ys in all_y:
        try:
            charts[key], src = get_rows(key, ys, mk)
            if mk == "tw" and key != "TWD=X" and src != "twse":
                warnings.append(f"{key} 證交所無法連線，改用 Yahoo")
        except Exception as e:
            errors[key] = str(e)
            warnings.append(f"{key} 取價失敗")

    if not charts:
        sys.exit("錯誤：所有標的取價失敗：" + json.dumps(errors, ensure_ascii=False))
    # 交易日曆：所有標的日期聯集，避免單一來源缺日造成 T 判錯
    cal = sorted({r["date"] for k, v in charts.items() if k != "TWD=X" for r in v})
    T, now = target_date(mk, [{"date": d} for d in cal])
    prevs = [d for d in cal if d < T]
    P = prevs[-1] if prevs else None
    ymap = dict(all_y)
    for key in list(charts):
        if key == "TWD=X":
            continue
        dates = {r["date"] for r in charts[key]}
        if T in dates and (P is None or P in dates):
            continue
        try:
            alt = alt_rows(key, ymap.get(key, key), mk)
        except Exception:
            alt = None
        if alt:
            merged = {r["date"]: r for r in alt[0]}
            merged.update({r["date"]: r for r in charts[key]})
            charts[key] = [merged[k] for k in sorted(merged)]
            dates = set(merged)
        if T not in dates or (P and P not in dates):
            warnings.append(f"{key} 缺 {T if T not in dates else P} 資料（兩個來源皆缺）")

    fx = charts["TWD=X"][-1]["close"] if charts.get("TWD=X") else None
    if mk == "us" and not a.symbols and (fx is None or not 25 < fx < 40):
        warnings.append(f"USD/TWD 匯率異常或缺漏（{fx}）")

    # ---- 只取價模式 ----
    if a.symbols:
        out = []
        for s in syms:
            cur, _ = pick(charts.get(s, []), T, P)
            if cur:
                out.append({"代號": s, "收盤價": round(cur["close"], 2), "日期": T.isoformat()})
            else:
                warnings.append(f"{s} 無 {T} 收盤")
        if a.prices_out:
            with open(a.prices_out, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=1)
        print(json.dumps({"T": T.isoformat(), "prices": out, "warnings": warnings}, ensure_ascii=False, indent=1))
        return

    # ---- 指數 ----
    index_rows = []
    for s, name in idx:
        cur, prev = pick(charts.get(s, []), T, P)
        if cur and prev:
            ch = cur["close"] - prev["close"]
            index_rows.append({"name": name, "close": cur["close"], "chg": ch, "pct": ch / prev["close"] * 100})
        else:
            warnings.append(f"{name} 無 {T} 收盤")

    # ---- 持股損益 ----
    rows, tot = [], {"day": 0.0, "pnl": 0.0, "mv": 0.0, "cost": 0.0}
    for h in holdings:
        s, sh, cost = h["symbol"], float(h["shares"]), float(h["cost"])
        cur, prev = pick(charts.get(s, []), T, P)
        r = {"symbol": s, "shares": sh, "cost": cost, "close": None, "prev": None,
             "day_pct": None, "day_pnl": None, "ret_pct": None, "pnl": None, "mv": None, "high": None}
        if not cur:
            warnings.append(f"{s} 缺 {T} 收盤，未計入加總")
        else:
            r["close"], r["high"] = cur["close"], cur["high"]
            r["mv"] = sh * cur["close"]
            r["pnl"] = sh * (cur["close"] - cost)
            r["ret_pct"] = (cur["close"] / cost - 1) * 100 if cost else None
            tot["mv"] += r["mv"]; tot["pnl"] += r["pnl"]; tot["cost"] += sh * cost
            if prev:
                r["prev"] = prev["close"]
                r["day_pnl"] = sh * (cur["close"] - prev["close"])
                r["day_pct"] = (cur["close"] / prev["close"] - 1) * 100
                tot["day"] += r["day_pnl"]
        rows.append(r)

    # ---- 合理性檢查 ----
    by = {r["symbol"]: r for r in rows}
    if mk == "tw" and by.get("00631L", {}).get("day_pct") is not None and by.get("0050", {}).get("day_pct") is not None:
        exp, got = by["0050"]["day_pct"] * 2, by["00631L"]["day_pct"]
        if abs(got - exp) > 1.0:
            warnings.append(f"00631L 漲跌 {got:+.2f}% 與 0050×2（{exp:+.2f}%）偏差過大，可能為舊價")
    for r in rows:
        limit = 25 if mk == "us" else (21 if r["symbol"].endswith("L") else 10.5)  # 台股槓桿ETF漲跌幅約2倍
        if r["day_pct"] is not None and abs(r["day_pct"]) > limit:
            warnings.append(f"{r['symbol']} 單日漲跌 {r['day_pct']:+.2f}% 異常，請人工確認")

    # ---- 輸出 ----
    cur_unit = "USD" if mk == "us" else "TWD"
    dname = "一二三四五六日"[T.weekday()]
    lines = [f"交易日：{'美東' if mk == 'us' else '台北'} {T:%Y/%m/%d}（週{dname}）收盤｜執行時間 {datetime.now(TPE):%Y/%m/%d %H:%M} 台北",
             ""]
    for x in index_rows:
        lines.append(f"- {x['name']}：{fmt(x['close'], 2)}（{x['chg']:+,.2f}，{x['pct']:+.2f}%）")
    lines += ["", f"| 持股 | 股數 | 成本({cur_unit}) | 收盤({cur_unit}) | 當日漲跌% | 當日損益({cur_unit}) | 總報酬% | 總損益({cur_unit}) | 市值({cur_unit}) |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        lines.append(f"| {r['symbol']} | {fmt(r['shares'], 1 if r['shares'] % 1 else 0)} | {fmt(r['cost'], 2)} | {fmt(r['close'], 2)} | "
                     f"{pct(r['day_pct'])} | {fmt(r['day_pnl'])} | {pct(r['ret_pct'])} | {fmt(r['pnl'])} | {fmt(r['mv'])} |")
    tot_ret = (tot["pnl"] / tot["cost"] * 100) if tot["cost"] else None
    lines.append(f"| **合計** | | | | | **{fmt(tot['day'])}** | **{pct(tot_ret)}** | **{fmt(tot['pnl'])}** | **{fmt(tot['mv'])}** |")
    if mk == "us" and fx:
        lines.append(f"\n折合新台幣（USD/TWD {fx:.3f}）：當日 {fmt(tot['day'] * fx)}｜總損益 {fmt(tot['pnl'] * fx)}｜市值 {fmt(tot['mv'] * fx)}")
    lines.append("\n檢查：" + ("全部通過" if not warnings else "；".join(warnings)))
    print("\n".join(lines))

    result = {"market": mk, "T": T.isoformat(), "fx_usdtwd": fx, "using_fallback": using_fallback,
              "indices": index_rows, "holdings": rows, "totals": tot, "total_ret_pct": tot_ret,
              "warnings": warnings, "errors": errors}
    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1, default=str)


if __name__ == "__main__":
    import urllib.parse  # noqa: F401
    main()
