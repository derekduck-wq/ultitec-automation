#!/usr/bin/env python3
"""
order_pivot.py — ULTITEC 接單統計表樞紐分析（月份／客戶／業務）

用法：
  python3 order_pivot.py orders.xlsx
  python3 order_pivot.py orders.xlsx --prev-total 1000000 --json-out pivot.json --md-out pivot.md

輸入：每月一個分頁（JAN…DEC 或 1月…12月），表頭列含「序號／客戶簡稱／業務／接單金額」。
  - 逐筆訂單 = 表頭下方「序號為數字」的連續列；遇到第一列序號非數字（空白分隔列）即停止，
    不會掃到下方的客戶小計與「每月出貨數量」彙總表。
  - 接單金額可能是數字或 "$22,249.45" 文字，一律轉成數字。
  - 業務、客戶名稱去除前後空白。
檢查：
  - 每月逐筆加總 vs 該分頁自己的「合計」列金額與客戶數（不一致即警告）
  - 與 --prev-total（上次 Notion 記錄的累計）比較：不應減少；超過 1.6 倍視為異常
  - 業務姓名大小寫變體、未知月份分頁
輸出：stdout 印出 Markdown 三張表與檢查結果；--md-out 存 Notion 用 Markdown；--json-out 存 JSON。
"""
import argparse
import json
import re
import sys
from collections import OrderedDict, defaultdict
from datetime import datetime

import openpyxl

MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def month_of(sheet_name):
    s = sheet_name.strip().upper()
    if s[:3] in MONTHS:
        return MONTHS[s[:3]]
    m = re.match(r"^(\d{1,2})\s*月", sheet_name.strip())
    return int(m.group(1)) if m else None


def to_num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace("$", "").replace(",", "").replace("US", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def is_num(v):
    return to_num(v) is not None


def parse_sheet(ws):
    rows = list(ws.iter_rows(values_only=True))
    hdr_i, cols = None, {}
    for i, r in enumerate(rows[:15]):
        cells = [str(c).strip() if c is not None else "" for c in r]
        if "序號" in cells and "接單金額" in cells:
            hdr_i = i
            for j, c in enumerate(cells):
                if c:
                    cols.setdefault(c, j)
            break
    if hdr_i is None:
        raise ValueError("找不到表頭列（序號／接單金額）")
    need = ["序號", "客戶簡稱", "業務", "接單金額"]
    miss = [k for k in need if k not in cols]
    if miss:
        raise ValueError(f"表頭缺欄位：{miss}")

    orders, bad_amount = [], []
    i = hdr_i + 1
    while i < len(rows):
        r = rows[i]
        seq = r[cols["序號"]] if cols["序號"] < len(r) else None
        if not is_num(seq):
            break  # 第一個非數字序號 = 訂單區結束
        amt = to_num(r[cols["接單金額"]])
        cust = str(r[cols["客戶簡稱"]] or "").strip()
        rep = str(r[cols["業務"]] or "").strip()
        o = {"seq": int(to_num(seq)), "customer": cust, "rep": rep, "amount": amt}
        if "訂單號" in cols:
            o["order_no"] = r[cols["訂單號"]]
        if "下單日期" in cols:
            d = r[cols["下單日期"]]
            o["date"] = d.date().isoformat() if isinstance(d, datetime) else (str(d) if d else None)
        if amt is None:
            bad_amount.append(o)
        orders.append(o)
        i += 1

    # 分頁自己的「合計」列：A 欄為「合計」，B=客戶數，D=金額（往下找第一個）
    sheet_total, sheet_count = None, None
    for r in rows[i:]:
        if r and str(r[0] or "").strip() == "合計":
            sheet_count = to_num(r[1]) if len(r) > 1 else None
            sheet_total = to_num(r[3]) if len(r) > 3 else None
            break
    return orders, bad_amount, sheet_total, sheet_count


def pct(v, tot):
    return f"{v / tot * 100:.1f}%" if tot else "—"


def money(v):
    return f"{v:,.2f}"


def table(title, rows, total, label):
    out = [f"## {title}", "", f"| {label} | 接單金額 | 佔比 |", "| --- | --- | --- |"]
    for k, v in rows:
        out.append(f"| {k} | {money(v)} | {pct(v, total)} |")
    out.append(f"| <b>小計</b> | <b>{money(total)}</b> | <b>100.0%</b> |")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx")
    ap.add_argument("--prev-total", type=float, help="上次記錄的累計金額（合理性檢查用）")
    ap.add_argument("--json-out")
    ap.add_argument("--md-out")
    a = ap.parse_args()

    wb = openpyxl.load_workbook(a.xlsx, data_only=True)
    warnings, months, all_orders = [], OrderedDict(), []
    for ws in wb.worksheets:
        m = month_of(ws.title)
        if m is None:
            if ws.title.strip() not in ("樞紐分析",):
                warnings.append(f"略過非月份分頁「{ws.title}」")
            continue
        try:
            orders, bad, st, sc = parse_sheet(ws)
        except ValueError as e:
            warnings.append(f"{ws.title} 解析失敗：{e}")
            continue
        if not orders:
            continue
        amt = sum(o["amount"] or 0 for o in orders)
        for o in bad:
            warnings.append(f"{ws.title} 序號{o['seq']}（{o['customer']}）接單金額無法辨識，未計入")
        if st is not None and abs(st - amt) > 0.01:
            warnings.append(f"{ws.title} 逐筆加總 {money(amt)} ≠ 分頁合計 {money(st)}（差 {money(amt - st)}）")
        n_cust = len({o["customer"] for o in orders})
        if sc is not None and int(sc) != n_cust:
            warnings.append(f"{ws.title} 客戶數 {n_cust} ≠ 分頁合計客戶數 {int(sc)}")
        if st is None:
            warnings.append(f"{ws.title} 找不到分頁合計列，無法交叉核對")
        for o in orders:
            o["month"] = m
        months[m] = {"sheet": ws.title, "amount": amt, "count": len(orders), "sheet_total": st}
        all_orders.extend(orders)

    if not all_orders:
        sys.exit("錯誤：沒有解析到任何訂單")

    total = round(sum(o["amount"] or 0 for o in all_orders), 2)
    by_cust, by_rep = defaultdict(float), defaultdict(float)
    for o in all_orders:
        by_cust[o["customer"]] += o["amount"] or 0
        by_rep[o["rep"]] += o["amount"] or 0

    # 業務名稱大小寫變體
    low = defaultdict(set)
    for r in by_rep:
        low[r.lower()].add(r)
    for v in low.values():
        if len(v) > 1:
            warnings.append(f"業務姓名疑似重複變體：{sorted(v)}")
    if "" in by_rep:
        warnings.append(f"有 {sum(1 for o in all_orders if not o['rep'])} 筆訂單缺業務")
    if "" in by_cust:
        warnings.append("有訂單缺客戶簡稱")

    if a.prev_total:
        if total < a.prev_total - 0.01:
            warnings.append(f"累計 {money(total)} 低於上次 {money(a.prev_total)}，可能有訂單被刪除或解析漏算")
        elif total > a.prev_total * 1.6:
            warnings.append(f"累計 {money(total)} 為上次 {money(a.prev_total)} 的 {total / a.prev_total:.2f} 倍，請檢查分頁邊界")

    m_rows = [(f"{m}月", v["amount"]) for m, v in months.items()]
    c_rows = sorted(by_cust.items(), key=lambda kv: -kv[1])
    r_rows = sorted(by_rep.items(), key=lambda kv: -kv[1])
    first, last = min(months), max(months)

    lines = []
    lines += table("月份統計", m_rows, total, "月份") + [""]
    lines += table("客戶統計", c_rows, total, "客戶") + [""]
    lines += table("業務統計", r_rows, total, "業務")
    summary = (f"累計 US${total:,.2f}｜{len(all_orders)} 筆訂單、{len(months)} 個月份（{first}–{last}月）、"
               f"{len(by_cust)} 個客戶、{len(by_rep)} 位業務")
    check = "檢查：" + ("全部通過（各月逐筆加總與分頁合計一致）" if not warnings else "；".join(warnings))

    print(summary)
    print()
    print("\n".join(lines))
    print()
    print(check)

    if a.md_out:
        with open(a.md_out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    if a.json_out:
        res = {"total": total, "orders": len(all_orders), "months": {str(k): v for k, v in months.items()},
               "first_month": first, "last_month": last,
               "customers": {k: round(v, 2) for k, v in c_rows}, "reps": {k: round(v, 2) for k, v in r_rows},
               "warnings": warnings,
               "last_month_amount": months[last]["amount"],
               "prev_month_amount": months.get(last - 1, {}).get("amount")}
        with open(a.json_out, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
