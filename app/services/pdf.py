from calendar import month_abbr
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

from app.utils import format_ugx
from app.services.reports import annual_contributor_monthly_breakdown


def _styles():
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="OrgTitle", fontSize=16, leading=20, spaceAfter=2))
    styles.add(ParagraphStyle(name="StatementTitle", fontSize=13, leading=16, spaceAfter=10, textColor=colors.HexColor("#333333")))
    return styles


def _statement_flowables(org_name, contributor, year, rows, styles):
    flow = []
    flow.append(Paragraph(org_name, styles["OrgTitle"]))
    flow.append(Paragraph(f"Annual Contribution Statement - {year}", styles["StatementTitle"]))
    flow.append(Paragraph(f"<b>Contributor:</b> {contributor.name}", styles["Normal"]))
    if contributor.phone:
        flow.append(Paragraph(f"<b>Phone:</b> {contributor.phone}", styles["Normal"]))
    flow.append(Spacer(1, 8))

    table_data = [["Month", "Mukululo", "Friday", "Sunday", "Total"]]
    totals = {"mukululo": 0, "friday": 0, "sunday": 0, "total": 0}
    for row in rows:
        table_data.append([
            month_abbr[row["month"]],
            format_ugx(row["mukululo"]),
            format_ugx(row["friday"]),
            format_ugx(row["sunday"]),
            format_ugx(row["total"]),
        ])
        for k in totals:
            totals[k] += row[k]
    table_data.append([
        "YEAR TOTAL",
        format_ugx(totals["mukululo"]),
        format_ugx(totals["friday"]),
        format_ugx(totals["sunday"]),
        format_ugx(totals["total"]),
    ])

    table = Table(table_data, colWidths=[28 * mm, 32 * mm, 32 * mm, 32 * mm, 32 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f4e3d")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eef3f0")),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
    ]))
    flow.append(table)
    flow.append(Spacer(1, 20))
    return flow


def generate_statement_pdf(org_name, contributor, year) -> bytes:
    rows = annual_contributor_monthly_breakdown(contributor.id, year)
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=18 * mm, bottomMargin=18 * mm)
    styles = _styles()
    flow = _statement_flowables(org_name, contributor, year, rows, styles)
    doc.build(flow)
    return buf.getvalue()


def generate_batch_statements_pdf(org_name, contributors, year) -> bytes:
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=18 * mm, bottomMargin=18 * mm)
    styles = _styles()
    flow = []
    for i, contributor in enumerate(contributors):
        rows = annual_contributor_monthly_breakdown(contributor.id, year)
        flow.extend(_statement_flowables(org_name, contributor, year, rows, styles))
        if i < len(contributors) - 1:
            flow.append(PageBreak())
    doc.build(flow)
    return buf.getvalue()
