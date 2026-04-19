"""Invoice PDF generation using WeasyPrint."""

from __future__ import annotations

import io
import logging
from pathlib import Path

from openboek.invoices.models import Invoice
from openboek.entities.models import Entity

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "invoices"


def generate_invoice_pdf(
    invoice: Invoice,
    entity: Entity,
    lang: str = "nl",
) -> bytes | None:
    """Generate a professional PDF for an invoice.

    Returns PDF bytes or None if WeasyPrint is not available.
    """
    try:
        from weasyprint import HTML
    except ImportError:
        logger.warning("WeasyPrint not installed — PDF generation unavailable")
        return None

    # Build HTML from template
    html_content = _render_invoice_html(invoice, entity, lang)

    try:
        pdf_bytes = HTML(string=html_content).write_pdf()
        return pdf_bytes
    except Exception as e:
        logger.exception("PDF generation failed: %s", e)
        return None


def _render_invoice_html(invoice: Invoice, entity: Entity, lang: str = "nl") -> str:
    """Render invoice as HTML for WeasyPrint."""
    is_nl = lang == "nl"

    # Build line items HTML
    lines_html = ""
    for line in invoice.lines:
        lines_html += f"""
        <tr>
            <td style="padding: 8px 12px; border-bottom: 1px solid #e5e7eb;">{line.description or '-'}</td>
            <td style="padding: 8px 12px; border-bottom: 1px solid #e5e7eb; text-align: right;">{line.quantity:.2f}</td>
            <td style="padding: 8px 12px; border-bottom: 1px solid #e5e7eb; text-align: right;">€{line.unit_price:.2f}</td>
            <td style="padding: 8px 12px; border-bottom: 1px solid #e5e7eb; text-align: right;">{line.btw_rate:.0f}%</td>
            <td style="padding: 8px 12px; border-bottom: 1px solid #e5e7eb; text-align: right;">€{line.btw_amount:.2f}</td>
            <td style="padding: 8px 12px; border-bottom: 1px solid #e5e7eb; text-align: right; font-weight: 500;">€{line.total:.2f}</td>
        </tr>"""

    label = {
        "invoice": "FACTUUR" if is_nl else "INVOICE",
        "number": "Factuurnummer" if is_nl else "Invoice number",
        "date": "Datum" if is_nl else "Date",
        "due_date": "Vervaldatum" if is_nl else "Due date",
        "to": "Aan" if is_nl else "To",
        "vat_number": "BTW-nummer" if is_nl else "VAT number",
        "description": "Omschrijving" if is_nl else "Description",
        "quantity": "Aantal" if is_nl else "Qty",
        "unit_price": "Prijs" if is_nl else "Price",
        "btw_rate": "BTW" if is_nl else "VAT",
        "btw_amount": "BTW bedrag" if is_nl else "VAT amount",
        "total": "Totaal" if is_nl else "Total",
        "subtotal": "Subtotaal" if is_nl else "Subtotal",
        "total_btw": "Totaal BTW" if is_nl else "Total VAT",
        "total_incl": "Totaal incl. BTW" if is_nl else "Total incl. VAT",
        "payment": "Betaling" if is_nl else "Payment",
        "iban": "IBAN",
        "kvk": "KVK",
        "tnv": "t.n.v." if is_nl else "in name of",
    }

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
    @page {{ size: A4; margin: 2cm; }}
    body {{ font-family: 'Helvetica Neue', Arial, sans-serif; font-size: 10pt; color: #1f2937; line-height: 1.5; }}
    .header {{ display: flex; justify-content: space-between; margin-bottom: 40px; }}
    .company {{ text-align: right; }}
    .company h1 {{ font-size: 16pt; margin: 0 0 4px 0; color: #059669; }}
    .company p {{ margin: 2px 0; font-size: 9pt; color: #6b7280; }}
    .invoice-title {{ font-size: 24pt; font-weight: 700; color: #059669; margin-bottom: 20px; }}
    .meta-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin-bottom: 32px; }}
    .meta-block h3 {{ font-size: 8pt; text-transform: uppercase; letter-spacing: 1px; color: #9ca3af; margin: 0 0 6px 0; }}
    .meta-block p {{ margin: 2px 0; }}
    table {{ width: 100%; border-collapse: collapse; margin-bottom: 24px; }}
    th {{ padding: 10px 12px; text-align: left; border-bottom: 2px solid #059669; font-size: 8pt;
          text-transform: uppercase; letter-spacing: 0.5px; color: #6b7280; }}
    th:not(:first-child) {{ text-align: right; }}
    .totals {{ float: right; width: 300px; }}
    .totals table {{ margin-bottom: 0; }}
    .totals td {{ padding: 6px 12px; }}
    .totals tr:last-child td {{ border-top: 2px solid #059669; font-weight: 700; font-size: 12pt; }}
    .payment-info {{ clear: both; margin-top: 48px; padding: 16px; background: #f9fafb; border-radius: 8px; }}
    .payment-info h3 {{ margin: 0 0 8px 0; font-size: 9pt; text-transform: uppercase; letter-spacing: 1px; color: #6b7280; }}
    .payment-info p {{ margin: 3px 0; font-size: 9pt; }}
</style>
</head>
<body>
    <div class="header">
        <div>
            <div class="invoice-title">{label['invoice']}</div>
        </div>
        <div class="company">
            <h1>{entity.name}</h1>
            <p>{entity.address or ''}</p>
            <p>{entity.city or ''}</p>
            <p>{label['kvk']}: {entity.kvk_number or '-'}</p>
            <p>BTW: {entity.btw_number or '-'}</p>
        </div>
    </div>

    <div class="meta-grid">
        <div class="meta-block">
            <h3>{label['to']}</h3>
            <p style="font-weight: 600;">{invoice.counterparty_name}</p>
            <p>{label['vat_number']}: {invoice.counterparty_vat or '-'}</p>
        </div>
        <div class="meta-block" style="text-align: right;">
            <h3>{label['number']}</h3>
            <p style="font-weight: 600;">{invoice.invoice_number}</p>
            <p>{label['date']}: {invoice.date}</p>
            <p>{label['due_date']}: {invoice.due_date or '-'}</p>
        </div>
    </div>

    <table>
        <thead>
            <tr>
                <th>{label['description']}</th>
                <th>{label['quantity']}</th>
                <th>{label['unit_price']}</th>
                <th>{label['btw_rate']}</th>
                <th>{label['btw_amount']}</th>
                <th>{label['total']}</th>
            </tr>
        </thead>
        <tbody>
            {lines_html}
        </tbody>
    </table>

    <div class="totals">
        <table>
            <tr>
                <td>{label['subtotal']}</td>
                <td style="text-align: right;">€{invoice.total_excl:.2f}</td>
            </tr>
            <tr>
                <td>{label['total_btw']}</td>
                <td style="text-align: right;">€{invoice.total_btw:.2f}</td>
            </tr>
            <tr>
                <td>{label['total_incl']}</td>
                <td style="text-align: right;">€{invoice.total_incl:.2f}</td>
            </tr>
        </table>
    </div>

    <div class="payment-info">
        <h3>{label['payment']}</h3>
        <p>{label['iban']}: {getattr(entity, 'iban', '') or '-'}</p>
        <p>{label['tnv']} {entity.name}</p>
    </div>
</body>
</html>"""
