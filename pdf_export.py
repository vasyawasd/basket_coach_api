"""Server-side PDF export of generated training plans (Cyrillic-capable)."""
import html
import io
import os
from typing import Any, Dict, Optional, Tuple

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

# Cyrillic font candidates across Windows, Linux, and macOS
_FONT_CANDIDATES = [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
    ("C:\\Windows\\Fonts\\arial.ttf",
     "C:\\Windows\\Fonts\\arialbd.ttf"),
    ("/Library/Fonts/Arial.ttf",
     "/Library/Fonts/Arial Bold.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf",
     "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
]

_ACCENT = colors.HexColor("#ff7a1a")
_HEADER_BG = colors.HexColor("#1a1a2e")
_MUTED = colors.HexColor("#666666")

_FONTS_REGISTERED = False


def _register_fonts() -> Optional[Tuple[str, str]]:
    global _FONTS_REGISTERED
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if _FONTS_REGISTERED or "PlanFont" in pdfmetrics.getRegisteredFontNames():
        return "PlanFont", "PlanFont-Bold"

    for regular, bold in _FONT_CANDIDATES:
        if os.path.exists(regular) and os.path.exists(bold):
            try:
                pdfmetrics.registerFont(TTFont("PlanFont", regular))
                pdfmetrics.registerFont(TTFont("PlanFont-Bold", bold))
                _FONTS_REGISTERED = True
                return "PlanFont", "PlanFont-Bold"
            except Exception:
                continue
    return None


def _txt(v: Any) -> str:
    """Escapes XML entities and preserves line breaks for ReportLab Paragraphs."""
    if v is None:
        return "—"
    text = str(v).strip()
    if not text:
        return "—"
    return html.escape(text, quote=True).replace("\n", "<br/>")


def generate_plan_pdf(payload: Dict[str, Any], api_result: Dict[str, Any]) -> bytes:
    """Builds a branded one-page-ish PDF with the training plan."""
    fonts = _register_fonts()
    font, font_bold = fonts if fonts else ("Helvetica", "Helvetica-Bold")

    styles = {
        "title": ParagraphStyle("title", fontName=font_bold, fontSize=20, textColor=_HEADER_BG, spaceAfter=2),
        "sub": ParagraphStyle("sub", fontName=font, fontSize=9, textColor=_MUTED, spaceAfter=10),
        "h2": ParagraphStyle("h2", fontName=font_bold, fontSize=13, textColor=_ACCENT,
                             spaceBefore=12, spaceAfter=6),
        "body": ParagraphStyle("body", fontName=font, fontSize=10, leading=14),
        "cell": ParagraphStyle("cell", fontName=font, fontSize=9, leading=12, wordWrap="CJK"),
        "cellb": ParagraphStyle("cellb", fontName=font_bold, fontSize=9, leading=12, wordWrap="CJK"),
    }

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title="Программа тренировок — Hoop Pro AI",
                            leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm)
    story = []

    data = api_result.get("data") or {}
    p = payload or {}

    story.append(Paragraph("HOOP PRO AI — Программа тренировок", styles["title"]))
    params_line = " · ".join(filter(None, [
        f"{_txt(p.get('height'))} см" if p.get("height") else None,
        f"{_txt(p.get('weight'))} кг" if p.get("weight") else None,
        _txt(p.get("position")),
        f"{_txt(p.get('days_per_week'))} дн/нед",
    ]))
    story.append(Paragraph(f"Параметры игрока: {params_line}", styles["sub"]))

    summary = data.get("summary") or data.get("program_summary") or data.get("overview")
    if summary:
        story.append(Paragraph("Обзор программы", styles["h2"]))
        story.append(Paragraph(_txt(summary), styles["body"]))

    safety = data.get("safety_notes") or data.get("safety_guidelines") or data.get("safety")
    if safety:
        items = safety if isinstance(safety, list) else [safety]
        story.append(Paragraph("Безопасность", styles["h2"]))
        for it in items:
            story.append(Paragraph(f"• {_txt(it)}", styles["body"]))

    schedule = data.get("schedule") or data.get("weekly_schedule") or data.get("days") or []
    if isinstance(schedule, dict):
        schedule = [{"day": k, **(v if isinstance(v, dict) else {"focus": v})}
                    for k, v in schedule.items()]

    for idx, day in enumerate(schedule, 1):
        if not isinstance(day, dict):
            continue
        story.append(Spacer(1, 6))
        day_title = _txt(day.get("day") or day.get("title") or f"День {idx}")
        day_focus = day.get("focus") or day.get("topic")
        header_text = f"{day_title} — {_txt(day_focus)}" if day_focus else day_title
        story.append(Paragraph(header_text, styles["h2"]))

        exercises = day.get("exercises") or []
        if isinstance(exercises, dict):
            exercises = [{"name": k, **(v if isinstance(v, dict) else {"notes": v})}
                         for k, v in exercises.items()]

        rows = [[Paragraph("<font color='white'><b>Упражнение</b></font>", styles["cellb"]),
                 Paragraph("<font color='white'><b>Подходы</b></font>", styles["cellb"]),
                 Paragraph("<font color='white'><b>Повторы</b></font>", styles["cellb"]),
                 Paragraph("<font color='white'><b>Техника</b></font>", styles["cellb"])]]
        for ex in exercises:
            if isinstance(ex, str):
                ex = {"name": ex}
            if not isinstance(ex, dict):
                continue
            rows.append([
                Paragraph(_txt(ex.get("name") or ex.get("exercise") or ex.get("title")), styles["cellb"]),
                Paragraph(_txt(ex.get("sets")), styles["cell"]),
                Paragraph(_txt(ex.get("reps")), styles["cell"]),
                Paragraph(_txt(ex.get("notes") or ex.get("tempo") or ex.get("description")), styles["cell"]),
            ])

        t = Table(rows, colWidths=[55 * mm, 20 * mm, 25 * mm, 80 * mm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), _HEADER_BG),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("ALIGN", (0, 0), (-1, -1), "LEFT"),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#f8f8fb"), colors.white]),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e2ec")),
        ]))
        story.append(t)

    doc.build(story)
    return buf.getvalue()
