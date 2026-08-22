"""Server-side PDF export of generated training plans (Cyrillic-capable)."""
import io
from typing import Any, Dict, Optional, Tuple

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

# First available Cyrillic font wins: DejaVu (Linux/Docker) or Arial (Windows)
_FONT_CANDIDATES = [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("C:\\Windows\\Fonts\\arial.ttf", "C:\\Windows\\Fonts\\arialbd.ttf"),
]

_ACCENT = colors.HexColor("#ff7a1a")
_HEADER_BG = colors.HexColor("#1a1a2e")
_MUTED = colors.HexColor("#666666")


def _register_fonts() -> Optional[Tuple[str, str]]:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for regular, bold in _FONT_CANDIDATES:
        try:
            pdfmetrics.registerFont(TTFont("PlanFont", regular))
            pdfmetrics.registerFont(TTFont("PlanFont-Bold", bold))
            return "PlanFont", "PlanFont-Bold"
        except Exception:
            continue
    return None


import html


def _txt(v: Any) -> str:
    if v is None:
        return "—"
    text = str(v).strip()
    if not text:
        return "—"
    return html.escape(text, quote=True)


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
        "cell": ParagraphStyle("cell", fontName=font, fontSize=9, leading=12),
        "cellb": ParagraphStyle("cellb", fontName=font_bold, fontSize=9, leading=12),
    }

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title="Программа тренировок — Hoop Pro AI",
                            leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm)
    story = []

    data = api_result.get("data") or {}
    p = payload or {}

    story.append(Paragraph("🏀 HOOP PRO AI — Программа тренировок", styles["title"]))
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
        story.append(Paragraph(f"{_txt(day.get('day') or day.get('title') or f'День {idx}')} — "
                               f"{_txt(day.get('focus') or day.get('topic'))}", styles["h2"]))

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
                Paragraph(_txt(ex.get("reps") or ex.get("duration")), styles["cell"]),
                Paragraph(_txt(ex.get("notes") or ex.get("instruction") or ex.get("description")), styles["cell"]),
            ])

        table = Table(rows, colWidths=[55 * mm, 20 * mm, 25 * mm, 80 * mm], repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), _HEADER_BG),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f5")]),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#dddddd")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(table)

    doc.build(story)
    return buf.getvalue()

