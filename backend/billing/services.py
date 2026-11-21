"""Monthly billing run.

Invoice generation is synchronous and happens inside one transaction.

Email delivery is not: the run only writes `pending` InvoiceDelivery rows (the
outbox) and makes one send attempt after commit. Anything not sent there is
picked up by `manage.py send_invoice_emails`.
"""
import datetime
import logging
import re
from decimal import ROUND_HALF_UP, Decimal
from itertools import groupby
from operator import attrgetter

from dateutil.relativedelta import relativedelta
from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from accounts.models import Company
from contracts.models import TimesheetEntry

from .delivery import deliver_invoices
from .models import BillingRun, Invoice, InvoiceDelivery, InvoiceLine

logger = logging.getLogger(__name__)

HOURS_PER_DAY = Decimal('8')
PENNY = Decimal('0.01')

_MONTH_RE = re.compile(r'(\d{4})-(\d{2})', re.ASCII)


class InvalidMonth(ValueError):
    """The month parameter is missing, malformed or not billable."""


def line_amount(hours: Decimal, daily_rate: Decimal) -> Decimal:
    """Bill hours proportionally against an 8-hour day, rounded to the penny."""
    return (hours / HOURS_PER_DAY * daily_rate).quantize(PENNY, rounding=ROUND_HALF_UP)


def parse_month(value: str | None) -> datetime.date:
    """Parse 'YYYY-MM' into the first day of that month. None (missing) is invalid."""
    match = _MONTH_RE.fullmatch(value) if value is not None else None
    if not match:
        raise InvalidMonth("Month must be in 'YYYY-MM' format.")
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12 or year < 1:
        raise InvalidMonth("Month must be in 'YYYY-MM' format.")
    return datetime.date(year, month, 1)


def parse_billable_month(value: str | None) -> datetime.date:
    """Parse a month for a billing run: only completed months can be billed.

    Prevent billing current month to avoid locking out future entries being invoiced.
    """
    period = parse_month(value)
    current = timezone.localdate().replace(day=1)
    if period >= current:
        raise InvalidMonth(
            f'Only completed months can be billed. {period:%B %Y} has not finished yet.'
        )
    return period


def next_month(period: datetime.date) -> datetime.date:
    """First day of the month after `period`."""
    return period + relativedelta(months=1, day=1)


def run_billing(*, company: Company, triggered_by: User, period: datetime.date) -> BillingRun:
    """Create one invoice per freelancer with approved entries in `period`.

    Idempotent per (company, freelancer, period): freelancers that already have
    an invoice for the period are counted as existing and left untouched.

    Raises IntegrityError, rolling back the whole run, if a selected entry is
    already on an invoice.
    """
    with transaction.atomic():
        # Ensure multiple triggers do not try and race each other. Successive attempts wait.
        Company.objects.select_for_update().get(pk=company.pk)
        company.refresh_from_db()

        run = BillingRun.objects.create(company=company, period=period, triggered_by=triggered_by)

        entries = (
            TimesheetEntry.objects.filter(
                status=TimesheetEntry.STATUS_APPROVED,
                date__gte=period,
                date__lt=next_month(period),
                contract__company=company,
                hours__gt=0,  # Skip 0 hours to avoid empty lines.
            )
            .select_related('contract__freelancer__user')
            .order_by('contract__freelancer_id', 'date', 'id')
        )
        already_invoiced = set(
            Invoice.objects.filter(company=company, period=period).values_list(
                'freelancer_id', flat=True
            )
        )

        new_invoice_ids = []
        for freelancer_id, group in groupby(entries, key=attrgetter('contract.freelancer_id')):
            if freelancer_id in already_invoiced:
                run.invoices_existing += 1
                continue
            invoice = _issue_invoice(run=run, company=company, period=period, entries=list(group))
            new_invoice_ids.append(invoice.pk)

        run.invoices_generated = len(new_invoice_ids)
        run.save(update_fields=['invoices_generated', 'invoices_existing'])

        if new_invoice_ids:
            # Never send email inside the transaction. robust=True: a failure
            # in the post-commit send must not turn a committed run into a 500.
            def send_new_invoices() -> None:
                deliver_invoices(new_invoice_ids)

            transaction.on_commit(send_new_invoices, robust=True)

    logger.info(
        'Billing run %s for company %s %s: %s generated, %s existing',
        run.pk, company.pk, f'{period:%Y-%m}', run.invoices_generated, run.invoices_existing,
    )
    return run


def _issue_invoice(
    *, run: BillingRun, company: Company, period: datetime.date, entries: list[TimesheetEntry]
) -> Invoice:
    freelancer = entries[0].contract.freelancer  # Grouped by freelancer so all entries should be the same
    lines = [
        InvoiceLine(
            timesheet_entry=entry,
            contract=entry.contract,
            date=entry.date,
            hours=entry.hours,
            daily_rate=entry.contract.daily_rate,
            amount=line_amount(entry.hours, entry.contract.daily_rate),
        )
        for entry in entries
    ]
    invoice = Invoice.objects.create(
        company=company,
        freelancer=freelancer,
        billing_run=run,
        period=period,
        reference=Invoice.build_reference(
            period=period, company_id=company.pk, freelancer_id=freelancer.pk
        ),
        total=sum((line.amount for line in lines), Decimal('0.00')),
        company_name=company.name,
        company_billing_email=company.billing_email,
        freelancer_name=freelancer.name,
    )
    for line in lines:
        line.invoice = invoice
    InvoiceLine.objects.bulk_create(lines)
    InvoiceDelivery.objects.bulk_create([
        InvoiceDelivery(
            invoice=invoice,
            recipient_type=InvoiceDelivery.RECIPIENT_COMPANY,
            email=company.billing_email,
        ),
        InvoiceDelivery(
            invoice=invoice,
            recipient_type=InvoiceDelivery.RECIPIENT_FREELANCER,
            email=freelancer.user.email,
        ),
    ])
    return invoice
