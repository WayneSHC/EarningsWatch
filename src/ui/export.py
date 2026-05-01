"""
src/ui/export.py
匯出功能：CSV（utf-8-sig）與 PDF（fpdf2 + STHeiti 中文字型）。

提供：
  - to_csv_single()        單公司矛盾 + 承諾 → CSV bytes
  - to_csv_compare()       多公司比較表 → CSV bytes
  - to_pdf_single()        單公司完整報告 → PDF bytes
  - to_pdf_compare()       多公司比較摘要 + 各公司報告 → PDF bytes
"""

from __future__ import annotations

import csv
import io
from datetime import date

# ── 字型路徑（macOS 內建 STHeiti；若不存在則 fallback 到英文）──────────────
_FONT_PATH = "/System/Library/Fonts/STHeiti Light.ttc"
_FONT_NAME = "STHeiti"


def _new_pdf():
    """建立已載入中文字型的 FPDF 物件。"""
    from fpdf import FPDF
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    try:
        pdf.add_font(_FONT_NAME, "", _FONT_PATH)
        pdf.add_font(_FONT_NAME, "B", _FONT_PATH)   # bold 也用同一字型
        _font = _FONT_NAME
    except Exception:
        _font = "Helvetica"           # 無中文字型時降級
    return pdf, _font


def _pdf_title(pdf, font: str, text: str, size: int = 16) -> None:
    pdf.set_font(font, size=size)
    pdf.set_text_color(30, 30, 30)
    pdf.multi_cell(0, 9, _strip_emoji(text), align="L")
    pdf.ln(2)


def _pdf_h2(pdf, font: str, text: str) -> None:
    pdf.set_font(font, size=12)
    pdf.set_text_color(50, 50, 50)
    pdf.multi_cell(0, 7, _strip_emoji(text), align="L")
    pdf.ln(1)


_EMOJI_MAP = {
    "✅": "[OK]", "❌": "[X]", "⚠": "[!]", "✓": "[OK]",
    "🚨": "[!!]", "📄": "", "📋": "", "📈": "", "📅": "",
}

def _strip_emoji(text: str) -> str:
    """把 STHeiti 不支援的 emoji 換成 ASCII 替代文字。"""
    for emoji, repl in _EMOJI_MAP.items():
        text = text.replace(emoji, repl)
    return text


def _pdf_body(pdf, font: str, text: str, size: int = 10) -> None:
    pdf.set_font(font, size=size)
    pdf.set_text_color(60, 60, 60)
    # 限制行寬避免溢出；fpdf2 multi_cell 自動換行
    pdf.multi_cell(0, 6, _strip_emoji(text), align="L")
    pdf.ln(1)


def _pdf_divider(pdf) -> None:
    pdf.set_draw_color(200, 200, 200)
    pdf.line(pdf.get_x(), pdf.get_y(), pdf.get_x() + 170, pdf.get_y())
    pdf.ln(3)


# ═══════════════════════════════════════════════════════════════════════════════
# CSV 匯出
# ═══════════════════════════════════════════════════════════════════════════════

def to_csv_single(result: dict, company: str, topic: str) -> bytes:
    """
    單公司：矛盾偵測結果 + 承諾追蹤 → UTF-8 BOM CSV bytes。
    Excel 在 Windows 上開啟中文不亂碼需要 BOM。
    """
    buf = io.StringIO()
    writer = csv.writer(buf)

    # ── 矛盾偵測 ────────────────────────────────────────────────────
    writer.writerow(["# 矛盾偵測結果"])
    writer.writerow(["公司", "主題", "匯出日期"])
    writer.writerow([company, topic, str(date.today())])
    writer.writerow([])
    writer.writerow(["季度A", "季度B", "立場變化", "有明確矛盾", "詳細說明",
                     "早期證據", "後期證據", "建議追問"])

    for c in result.get("contradictions", []):
        a = c.get("analysis", {})
        writer.writerow([
            c.get("quarter_a", ""),
            c.get("quarter_b", ""),
            a.get("stance_change", ""),
            "是" if a.get("has_contradiction") else "否",
            a.get("change_detail", ""),
            a.get("evidence_early", ""),
            a.get("evidence_later", ""),
            a.get("follow_up_question", ""),
        ])

    writer.writerow([])

    # ── 承諾追蹤 ────────────────────────────────────────────────────
    writer.writerow(["# 承諾追蹤"])
    writer.writerow(["承諾季度", "承諾內容", "後續季度", "兌現狀況", "說明"])
    for p in result.get("promises", []):
        writer.writerow([
            p.get("promise_quarter", ""),
            p.get("content", ""),
            p.get("followup_quarter", ""),
            p.get("status", ""),
            p.get("detail", ""),
        ])

    return buf.getvalue().encode("utf-8-sig")


def to_csv_compare(
    comparison_table: list[dict],
    companies: list[str],
    topic: str,
) -> bytes:
    """多公司比較表 → CSV bytes。"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["# 多公司跨季立場比較"])
    writer.writerow(["主題", topic, "匯出日期", str(date.today())])
    writer.writerow([])
    writer.writerow(["季度對"] + companies)
    for row in comparison_table:
        writer.writerow([row.get("quarter_pair", "")] + [row.get(c, "—") for c in companies])
    return buf.getvalue().encode("utf-8-sig")


# ═══════════════════════════════════════════════════════════════════════════════
# PDF 匯出
# ═══════════════════════════════════════════════════════════════════════════════

def to_pdf_single(result: dict, company: str, topic: str) -> bytes:
    """單公司完整報告 → PDF bytes。"""
    pdf, font = _new_pdf()
    pdf.add_page()

    # 標題
    _pdf_title(pdf, font, f"EarningsWatch 分析報告")
    _pdf_h2(pdf, font, f"公司：{company}　主題：{topic}　日期：{date.today()}")
    _pdf_divider(pdf)

    # 信心度摘要
    confidence = result.get("confidence", 0.0)
    contradictions = result.get("contradictions", [])
    promises = result.get("promises", [])
    n_shift = sum(
        1 for c in contradictions
        if c.get("analysis", {}).get("stance_change") not in ("維持不變", "無關", None)
    )
    n_contra = sum(1 for c in contradictions if c.get("analysis", {}).get("has_contradiction"))
    _pdf_body(pdf, font,
              f"信心度：{confidence:.0%}　跨季比對：{len(contradictions)} 組　"
              f"立場轉變：{n_shift} 處　明確矛盾：{n_contra} 處")
    _pdf_divider(pdf)

    # 完整報告
    _pdf_title(pdf, font, "完整分析報告", size=13)
    report_text = result.get("final_report", "（無報告內容）")
    _pdf_body(pdf, font, report_text)
    _pdf_divider(pdf)

    # 矛盾詳情
    relevant = [
        c for c in contradictions
        if c.get("analysis", {}).get("stance_change") not in ("無關", None)
    ]
    if relevant:
        _pdf_title(pdf, font, f"矛盾 / 立場轉變詳情（共 {len(relevant)} 組）", size=13)
        for c in relevant:
            a = c.get("analysis", {})
            q_a = c.get("quarter_a", "")
            q_b = c.get("quarter_b", "")
            stance = a.get("stance_change", "")
            detail = a.get("change_detail", "")
            ev_e = a.get("evidence_early", "")
            ev_l = a.get("evidence_later", "")
            follow = a.get("follow_up_question", "")

            icon = "⚠" if a.get("has_contradiction") else "✓"
            _pdf_h2(pdf, font, f"{icon} {q_a} vs {q_b}　立場：{stance}")
            if detail:
                _pdf_body(pdf, font, detail, size=9)
            if ev_e:
                _pdf_body(pdf, font, f"  早期：「{ev_e[:120]}」", size=9)
            if ev_l:
                _pdf_body(pdf, font, f"  後期：「{ev_l[:120]}」", size=9)
            if follow:
                _pdf_body(pdf, font, f"  建議追問：{follow}", size=9)
            pdf.ln(2)

    # 承諾追蹤
    if promises:
        _pdf_divider(pdf)
        _pdf_title(pdf, font, f"承諾追蹤（共 {len(promises)} 項）", size=13)
        for p in promises:
            status = p.get("status", "⚠")
            pq = p.get("promise_quarter", "")
            content = p.get("content", "")
            fq = p.get("followup_quarter", "")
            detail = p.get("detail", "")
            _pdf_h2(pdf, font, f"{status.split()[0]} [{pq}承諾] {content[:80]}")
            if detail:
                _pdf_body(pdf, font, f"  後續（{fq}）：{detail}", size=9)
            pdf.ln(1)

    # 頁腳
    _pdf_divider(pdf)
    _pdf_body(pdf, font,
              "EarningsWatch 是文件分析工具，不提供投資建議。"
              "資料來源：MOPS 公開資訊觀測站法說會逐字稿。",
              size=8)

    return bytes(pdf.output())


def to_pdf_compare(
    multi_results: dict[str, dict],
    comparison_table: list[dict],
    diff_summary: str,
    companies: list[str],
    topic: str,
) -> bytes:
    """多公司比較摘要 + 各公司報告 → PDF bytes。"""
    pdf, font = _new_pdf()
    pdf.add_page()

    # 標題
    _pdf_title(pdf, font, "EarningsWatch 多公司比較報告")
    _pdf_h2(pdf, font,
            f"公司：{' vs '.join(companies)}　主題：{topic}　日期：{date.today()}")
    _pdf_divider(pdf)

    # 各公司指標列
    for cname in companies:
        r = multi_results.get(cname, {})
        conf = r.get("confidence", 0.0)
        n_cont = len(r.get("contradictions", []))
        n_shift = sum(
            1 for c in r.get("contradictions", [])
            if c.get("analysis", {}).get("stance_change") not in ("維持不變", "無關", None)
        )
        _pdf_body(pdf, font,
                  f"{cname}：信心度 {conf:.0%}　跨季組數 {n_cont}　立場轉變 {n_shift} 處")
    _pdf_divider(pdf)

    # 跨季立場比較表
    if comparison_table:
        _pdf_title(pdf, font, "跨季立場對照表", size=13)
        header = ["季度對"] + companies
        col_w = 170 / len(header)
        pdf.set_font(font, size=9)
        pdf.set_fill_color(240, 240, 240)
        for h in header:
            pdf.cell(col_w, 7, h, border=1, fill=True)
        pdf.ln()
        for row in comparison_table:
            pdf.set_font(font, size=9)
            pdf.cell(col_w, 6, row.get("quarter_pair", ""), border=1)
            for c in companies:
                val = row.get(c, "—")
                fill = False
                if val == "更樂觀":
                    pdf.set_fill_color(212, 237, 218)
                    fill = True
                elif val == "更保守":
                    pdf.set_fill_color(248, 215, 218)
                    fill = True
                else:
                    pdf.set_fill_color(255, 255, 255)
                pdf.cell(col_w, 6, val, border=1, fill=fill)
            pdf.ln()
        pdf.ln(3)

    # 差異摘要
    if diff_summary:
        _pdf_title(pdf, font, "差異摘要", size=13)
        _pdf_body(pdf, font, diff_summary)
        _pdf_divider(pdf)

    # 各公司完整報告
    for cname in companies:
        r = multi_results.get(cname, {})
        if "error" in r:
            continue
        pdf.add_page()
        _pdf_title(pdf, font, f"{cname} — 分析報告", size=14)
        _pdf_body(pdf, font, r.get("final_report", "（無報告內容）"))

    # 頁腳
    _pdf_divider(pdf)
    _pdf_body(pdf, font,
              "EarningsWatch 是文件分析工具，不提供投資建議。"
              "資料來源：MOPS 公開資訊觀測站法說會逐字稿。",
              size=8)

    return bytes(pdf.output())
