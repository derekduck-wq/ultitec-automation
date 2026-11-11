#!/usr/bin/env python3
"""
portfolio_weekly.py — Jason 投資組合週報計算（Notion「投資每週變動儀表板」用）

用法：
  python3 portfolio_weekly.py --input weekly_in.json --json-out weekly.json --md-out weekly.md
  （測試／補算可加 --override prices.json：{"prices":{"ORCL":116.10,...},"fx":32.768,"us_date":"2026-11-06","tw_date":"2026-11-06"}）

weekly_in.json（由 Claude 從 Notion 持股主檔與上週頁面整理）：
{
  "fx_prev": 32.5,                         # 上週頁面匯率
  "prev_total_mv": 10000000,               # 上週頁面總市值（選填）
  "realized_twd_total": 500000,            # 總覽「已實現損益」原樣沿用
  "realized": {"台股": "NT$100,000", "國泰美股": "US$1,000.00"},  # 帳戶「已實現原幣」原樣沿用
  "us": [{"symbol": "NVDA", "base_price": 180.0, "base_date": "2026/10/30", "prev_shares": 100,
          "accounts": {"國泰": [60, 150.0], "SC": [40, 200.0]}}, ...],
  "tw": [{"symbol": "2330", "name": "台積電", "shares": 1000, "cost": 1000.0,
          "base_price": 2000, "base_date": "2026/10/30", "prev_shares": 1000}, ...],
  "fallback": "2026/10/28"                 # 選填：使用備用持股清單時填日期
}
帳戶名稱可用 國泰／國泰美股、富邦／富邦銀行、SC／渣打、UBS。

計算規則：
  本週金額變化 = 目前股數 ×（現價 − 上週基準價）（美股 × 目前匯率）
  帳戶成本、市值皆以目前匯率換算；本週% = 本週變化 ÷（市值 − 本週變化）
  匯兌效果 = 美股以基準價計的市值 ×（目前匯率 − 上週匯率），不計入本週變化
  週高 = 基準日之後各交易日最高價（美股，JSON 的 us[].week_high，供限價單檢查）
現價：台股以證交所為主、Yahoo 備援；美股 Yahoo；匯率 Yahoo TWD=X（最新一筆）。
"""
import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UA = {"User-Agent": "Mozilla/5.0"}
ET = ZoneInfo("America/New_York")
TPE = ZoneInfo("Asia/Taipei")
ACCT = {"國泰": "國泰美股", "國泰美股": "國泰美股", "富邦": "富邦銀行", "富邦銀行": "富邦銀行",
        "SC": "渣打", "渣打": "渣打", "UBS": "UBS"}
ACCT_ORDER = ["台股", "國泰美股", "富邦銀行", "UBS", "渣打"]
HIGHS = {}  # Yahoo 每日最高價，用於限價單週高檢查


def get_json(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def yahoo_rows(sym, rng="15d"):
    last = None
    for host in ("query1", "query2"):
        for _ in range(2):
            try:
                d = get_json(f"https://{host}.finance.yahoo.com/v8/finance/chart/"
                             f"{urllib.parse.quote(sym)}?range={rng}&interval=1d")
                res = d["chart"]["result"][0]
                off = res["meta"].get("gmtoffset", 0)
                q = res["indicators"]["quote"][0]
                rows = {}
                for ts, c, hi in zip(res.get("timestamp", []), q.get("close", []), q.get("high", [])):
                    if c is not None:
                        d = datetime.fromtimestamp(ts + off, tz=timezone.utc).date()
                        rows[d] = round(c, 4)
                        if hi is not None:
                            HIGHS.setdefault(sym, {})[d] = round(hi, 4)
                if rows:
                    return rows
            except Exception as e:  # noqa
                last = e
                time.sleep(2)
    raise RuntimeError(f"{sym} Yahoo 取價失敗：{last}")


def twse_rows(sym):
    today = datetime.now(TPE).date()
    rows = {}
    for mth in (today.replace(day=1), (today.replace(day=1) - timedelta(days=1)).replace(day=1)):
        j = get_json("https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?"
                     + urllib.parse.urlencode({"date": mth.strftime("%Y%m%d"), "stockNo": sym, "response": "json"}))
        if j.get("stat") != "OK":
            raise RuntimeError(j.get("stat"))
        for r in j.get("data", []):
            y, m, d = r[0].split("/")
            c = str(r[6]).replace(",", "").strip()
            if c not in ("", "--", "---"):
                rows[datetime(int(y) + 1911, int(m), int(d)).date()] = float(c)
        time.sleep(1)
    if not rows:
        raise RuntimeError(f"{sym} 證交所無資料")
    return rows


def closed_dates(rows, tz, hh, mm):
    now = datetime.now(tz)
    ok = (now.hour, now.minute) >= (hh, mm)
    return {d: v for d, v in rows.items() if d < now.date() or (d == now.date() and ok)}


def latest_close(rows_by_sym, tz, hh, mm, warnings, label):
    """以所有標的日期聯集找最近已收盤交易日 T，各標的取 T 日收盤。"""
    rows_by_sym = {k: closed_dates(v, tz, hh, mm) for k, v in rows_by_sym.items()}
    cal = sorted({d for v in rows_by_sym.values() for d in v})
    if not cal:
        raise RuntimeError(f"{label} 找不到已收盤交易日")
    T = cal[-1]
    out = {}
    for s, v in rows_by_sym.items():
        if T in v:
            out[s] = v[T]
        else:
            warnings.append(f"{s} 缺 {T} 收盤")
    return T, out


def fmt(n, d=0, sign=False):
    if n is None:
        return "—"
    s = f"{abs(n):,.{d}f}"
    if sign:
        return ("+" if n >= 0 else "−") + s
    return ("−" if n < 0 else "") + s


def pctf(n):
    return "—" if n is None else ("+" if n >= 0 else "−") + f"{abs(n):.2f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--override", help="測試用：固定現價與匯率")
    ap.add_argument("--json-out")
    ap.add_argument("--md-out")
    a = ap.parse_args()
    cfg = json.load(open(a.input, encoding="utf-8"))
    warnings = []
    us, tw = cfg.get("us", []), cfg.get("tw", [])
    if cfg.get("fallback"):
        warnings.append(f"⚠️ Notion持股主檔讀取失敗，本次使用備用舊資料（{cfg['fallback']}）")

    # ---- 現價與匯率 ----
    if a.override:
        ov = json.load(open(a.override, encoding="utf-8"))
        px, fx = ov["prices"], ov["fx"]
        us_T, tw_T = ov.get("us_date", "—"), ov.get("tw_date", "—")
    else:
        us_rows, tw_rows = {}, {}
        for h in us:
            try:
                us_rows[h["symbol"]] = yahoo_rows(h["symbol"])
            except Exception as e:
                warnings.append(f"{h['symbol']} 取價失敗：{e}")
        for h in tw:
            s = h["symbol"]
            try:
                tw_rows[s] = twse_rows(s)
            except Exception:
                try:
                    tw_rows[s] = yahoo_rows(s + ".TW")
                    warnings.append(f"{s} 證交所無法連線，改用 Yahoo")
                except Exception as e:
                    warnings.append(f"{s} 取價失敗：{e}")
        px = {}
        us_T = tw_T = None
        if us_rows:
            us_T, p = latest_close(us_rows, ET, 16, 15, warnings, "美股")
            px.update(p)
        if tw_rows:
            tw_T, p = latest_close(tw_rows, TPE, 14, 30, warnings, "台股")
            px.update(p)
        try:
            fxr = yahoo_rows("TWD=X", "5d")
            fx = fxr[max(fxr)]
        except Exception as e:
            fx = None
            warnings.append(f"USD/TWD 取價失敗：{e}")
        us_T = us_T.isoformat() if us_T else "—"
        tw_T = tw_T.isoformat() if tw_T else "—"
    if not fx or not 25 < fx < 40:
        sys.exit(f"錯誤：USD/TWD 匯率異常或缺漏（{fx}），停止計算以免寫入錯誤數字")
    fx_prev = cfg.get("fx_prev")

    accts = {k: {"mv": 0.0, "cost": 0.0, "chg": 0.0} for k in ACCT_ORDER}
    share_changes, no_base = [], []

    # ---- 台股 ----
    tw_out, tw_chg_sum = [], 0.0
    for h in tw:
        s, sh, cost = h["symbol"], float(h["shares"]), float(h["cost"])
        p, b = px.get(s), h.get("base_price")
        r = {"symbol": s, "name": h.get("name", ""), "shares": sh, "base_date": h.get("base_date"),
             "base": b, "price": p, "cost_total": sh * cost}
        if p is None:
            r.update(mv=None, chg=None, diff=None, pct=None, unreal=None)
        else:
            r["mv"] = sh * p
            r["unreal"] = (p / cost - 1) * 100 if cost else None
            if b:
                r["diff"], r["pct"] = p - b, (p / b - 1) * 100
                r["chg"] = sh * (p - b)
                tw_chg_sum += r["chg"]
            else:
                r.update(diff=None, pct=None, chg=None)
                no_base.append(s)
            accts["台股"]["mv"] += r["mv"]
        accts["台股"]["cost"] += r["cost_total"]
        if h.get("prev_shares") is not None and float(h["prev_shares"]) != sh:
            share_changes.append(f"{s} {fmt(float(h['prev_shares']))}→{fmt(sh)}")
        tw_out.append(r)
    accts["台股"]["chg"] = tw_chg_sum

    # ---- 美股（跨帳戶）----
    us_out, us_chg_sum, fx_effect = [], 0.0, 0.0
    for h in us:
        s, p, b = h["symbol"], px.get(h["symbol"]), h.get("base_price")
        tot_sh, names = 0.0, []
        for acc, (sh, c) in h["accounts"].items():
            an = ACCT.get(acc.strip())
            if an is None:
                warnings.append(f"{s} 帳戶「{acc}」無法對應，未計入")
                continue
            sh, c = float(sh), float(c)
            tot_sh += sh
            names.append(f"{an.replace('美股', '').replace('銀行', '')} {fmt(sh, 1 if sh % 1 else 0)}")
            accts[an]["cost"] += sh * c * fx
            if p is not None:
                accts[an]["mv"] += sh * p * fx
                if b:
                    accts[an]["chg"] += sh * (p - b) * fx
        r = {"symbol": s, "shares": tot_sh, "base_date": h.get("base_date"), "base": b, "price": p,
             "accounts": "／".join(names), "week_high": None}
        try:
            bd = datetime.strptime(str(h.get("base_date")).replace("-", "/"), "%Y/%m/%d").date()
            hs = [v for d, v in HIGHS.get(s, {}).items() if d > bd]
            r["week_high"] = max(hs) if hs else None
        except ValueError:
            pass
        if p is not None:
            r["mv_twd"] = tot_sh * p * fx
            if b:
                r["diff"], r["pct"] = p - b, (p / b - 1) * 100
                r["chg"] = tot_sh * (p - b) * fx
                us_chg_sum += r["chg"]
                if fx_prev:
                    fx_effect += tot_sh * b * (fx - fx_prev)
            else:
                r.update(diff=None, pct=None, chg=None)
                no_base.append(s)
        else:
            r.update(mv_twd=None, diff=None, pct=None, chg=None)
        if h.get("prev_shares") is not None and float(h["prev_shares"]) != tot_sh:
            share_changes.append(f"{s} {fmt(float(h['prev_shares']), 1)}→{fmt(tot_sh, 1)}")
        us_out.append(r)

    # ---- 加總與核對 ----
    for v in accts.values():
        v["unreal_pct"] = (v["mv"] / v["cost"] - 1) * 100 if v["cost"] else None
        base = v["mv"] - v["chg"]
        v["chg_pct"] = v["chg"] / base * 100 if base else None
    tot_mv = sum(v["mv"] for v in accts.values())
    tot_cost = sum(v["cost"] for v in accts.values())
    tot_chg = sum(v["chg"] for v in accts.values())
    tot_unreal = tot_mv - tot_cost
    chk_tw = abs(tw_chg_sum - accts["台股"]["chg"]) < 1
    us4 = sum(accts[k]["chg"] for k in ["國泰美股", "富邦銀行", "UBS", "渣打"])
    chk_us = abs(us_chg_sum - us4) < 1
    if not chk_tw:
        warnings.append("台股個股變化加總 ≠ 台股帳戶變化")
    if not chk_us:
        warnings.append(f"跨帳戶持股變化加總 {fmt(us_chg_sum)} ≠ 四帳戶變化 {fmt(us4)}")
    if no_base:
        warnings.append(f"無上週基準價（排除於變化加總）：{', '.join(no_base)}")
    if fx_prev is None:
        warnings.append("缺上週匯率，未計算匯兌效果")

    # ---- Markdown ----
    R = cfg.get("realized", {})
    L = []
    L += ["## 總覽（新台幣）", "", "| 項目 | 金額 |", "| --- | --- |",
          f"| 總市值 | NT${fmt(tot_mv)} |",
          f"| 本週變動 | <b>{'+' if tot_chg >= 0 else '−'}NT${fmt(abs(tot_chg))}（{pctf(tot_chg / (tot_mv - tot_chg) * 100)}）</b> |",
          f"| 未實現損益 | {'+' if tot_unreal >= 0 else '−'}NT${fmt(abs(tot_unreal))}（{pctf(tot_unreal / tot_cost * 100)}） |",
          f"| 已實現損益 | {('+NT$' + fmt(cfg['realized_twd_total'])) if cfg.get('realized_twd_total') is not None else '—'} |", ""]
    L += ["## 帳戶明細", "",
          "| 帳戶 | 市值NT$ | 成本NT$ | 未實現% | 本週金額變化(NT$) | 本週% | 已實現原幣 | 資料至 |",
          "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for k in ACCT_ORDER:
        v = accts[k]
        if v["mv"] == 0 and v["cost"] == 0:
            continue
        d = (tw_T if k == "台股" else us_T).replace("-", "/")
        L.append(f"| {k} | {fmt(v['mv'])} | {fmt(v['cost'])} | {pctf(v['unreal_pct'])} | {fmt(v['chg'], sign=True)} | "
                 f"{pctf(v['chg_pct'])} | {R.get(k, '—')} | {d} |")
    L.append(f"| <b>加總</b> | <b>{fmt(tot_mv)}</b> | <b>{fmt(tot_cost)}</b> | <b>{pctf(tot_unreal / tot_cost * 100)}</b> | "
             f"<b>{fmt(tot_chg, sign=True)}</b> | <b>{pctf(tot_chg / (tot_mv - tot_chg) * 100)}</b> | — | — |")
    L += ["", "## 台股個股明細（國泰證券）", "",
          "| 標的 | 股數 | 上次收盤日期 | 上次收盤價 | 現價 | 漲跌 | 漲跌% | 本週金額變化(NT$) | 成本 | 市值 | 未實現% |",
          "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in tw_out:
        L.append(f"| {r['symbol']} {r['name']} | {fmt(r['shares'])} | {r['base_date'] or '—'} | "
                 f"{'NT$' + fmt(r['base'], 2) if r['base'] else '—'} | {'NT$' + fmt(r['price'], 2) if r['price'] else '—'} | "
                 f"{('+' if (r['diff'] or 0) >= 0 else '−') + 'NT$' + fmt(abs(r['diff']), 2) if r['diff'] is not None else '—'} | "
                 f"{pctf(r['pct'])} | {('+' if (r['chg'] or 0) >= 0 else '−') + 'NT$' + fmt(abs(r['chg'])) if r['chg'] is not None else '—'} | "
                 f"NT${fmt(r['cost_total'])} | {'NT$' + fmt(r['mv']) if r['mv'] is not None else '—'} | {pctf(r['unreal'])} |")
    L.append(f"| <b>變化加總</b> |  |  |  |  |  |  | <b>{'+' if tw_chg_sum >= 0 else '−'}NT${fmt(abs(tw_chg_sum))}</b> |  |  |  |")
    L += ["", "## 跨帳戶重疊持股", "",
          "| 標的 | 股數 | 上次收盤日期 | 上次收盤價(US$) | 現價(US$) | 漲跌 | 漲跌% | 本週金額變化(NT$) | 市值約當NT$ | 帳戶 |",
          "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in us_out:
        L.append(f"| {r['symbol']} | {fmt(r['shares'], 1 if r['shares'] % 1 else 0)} | {r['base_date'] or '—'} | "
                 f"{'$' + fmt(r['base'], 2) if r['base'] else '—'} | {'$' + fmt(r['price'], 2) if r['price'] else '—'} | "
                 f"{('+' if (r['diff'] or 0) >= 0 else '−') + '$' + fmt(abs(r['diff']), 2) if r['diff'] is not None else '—'} | "
                 f"{pctf(r['pct'])} | {('+' if (r['chg'] or 0) >= 0 else '−') + 'NT$' + fmt(abs(r['chg'])) if r['chg'] is not None else '—'} | "
                 f"{'NT$' + fmt(r['mv_twd']) if r['mv_twd'] is not None else '—'} | {r['accounts']} |")
    L.append(f"| <b>變化加總</b> |  |  |  |  |  |  | <b>{'+' if us_chg_sum >= 0 else '−'}NT${fmt(abs(us_chg_sum))}</b> |  |  |")

    check = (f"核對：台股個股變化加總 {fmt(tw_chg_sum, sign=True)} {'＝' if chk_tw else '≠'} 台股帳戶本週變化；"
             f"跨帳戶持股變化加總 {fmt(us_chg_sum, sign=True)} {'＝' if chk_us else '≠'} 國泰美股+富邦+UBS+渣打 "
             f"（{'、'.join(k + ' ' + fmt(accts[k]['chg'], sign=True) for k in ['國泰美股', '富邦銀行', 'UBS', '渣打'])}）")
    print(f"美股資料至 {us_T}｜台股資料至 {tw_T}｜USD/TWD {fx:.3f}（上週 {fx_prev}）")
    print(f"總市值 NT${fmt(tot_mv)}｜本週 {fmt(tot_chg, sign=True)}（{pctf(tot_chg / (tot_mv - tot_chg) * 100)}）｜"
          f"未實現 {fmt(tot_unreal, sign=True)}｜匯兌效果 {fmt(fx_effect, sign=True) if fx_prev else '—'}（未計入本週變動）")
    if cfg.get("prev_total_mv"):
        print(f"總市值較上週頁面 {fmt(tot_mv - cfg['prev_total_mv'], sign=True)}（{fmt(cfg['prev_total_mv'])}→{fmt(tot_mv)}）")
    print("股數變動（本週變化混入買賣）：" + ("；".join(share_changes) if share_changes else "無"))
    print()
    print("\n".join(L))
    print()
    print(check)
    print("檢查：" + ("全部通過" if not warnings else "；".join(warnings)))

    if a.md_out:
        open(a.md_out, "w", encoding="utf-8").write("\n".join(L) + "\n")
    if a.json_out:
        movers = sorted([r for r in tw_out + us_out if r.get("chg") is not None], key=lambda r: abs(r["chg"]), reverse=True)
        json.dump({"us_date": us_T, "tw_date": tw_T, "fx": fx, "fx_prev": fx_prev, "fx_effect": fx_effect,
                   "total_mv": tot_mv, "total_cost": tot_cost, "total_chg": tot_chg,
                   "total_chg_pct": tot_chg / (tot_mv - tot_chg) * 100, "total_unreal": tot_unreal,
                   "accounts": accts, "tw": tw_out, "us": us_out, "top_movers": [
                       {"symbol": r["symbol"], "chg_twd": r["chg"], "pct": r["pct"]} for r in movers[:3]],
                   "share_changes": share_changes, "check_ok": chk_tw and chk_us, "warnings": warnings},
                  open(a.json_out, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)


if __name__ == "__main__":
    main()
