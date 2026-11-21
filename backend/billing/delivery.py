"""Invoice email delivery (the outbox sender).

Used by both the post-commit hook of a billing run and the
`send_invoice_emails` management command.

Delivery is at-least-once. If we fail to save that sending completed, it will retry.
"""
import datetime
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from django.conf import settings
from django.core.mail import EmailMessage
from django.db.models import F, Q
from django.utils import timezone

from .models import Invoice, InvoiceDelivery

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
STALE_SENDING_AFTER = datetime.timedelta(minutes=10)

MISSING_EMAIL_ERROR = 'No email address on record for this recipient.'
ABANDONED_ERROR = 'Delivery attempt never completed (sender stopped mid-send); attempts exhausted.'


def _claimable(now: datetime.datetime) -> Q:
    # Invoices can be sent if they:
    # Have no hit max attempts
    # Are pending, failed or have been stuck in sending for too long.
    return Q(attempts__lt=MAX_ATTEMPTS) & (
        Q(status__in=[InvoiceDelivery.STATUS_PENDING, InvoiceDelivery.STATUS_FAILED])
        | Q(status=InvoiceDelivery.STATUS_SENDING, last_attempt_at__lt=now - STALE_SENDING_AFTER)
    )


def attempt_delivery(delivery_id: int) -> str | None:
    """Claim and send one delivery. Returns the resulting status, or None if
    the row could not be claimed (already sent, in flight elsewhere, or out of
    attempts).

    `attempts` is incremented at claim time, so a sender that crashes
    mid-send still uses up an attempt and a row can't loop forever.
    """
    now = timezone.now()
    claimed = (
        InvoiceDelivery.objects.filter(_claimable(now), pk=delivery_id)
        .update(status=InvoiceDelivery.STATUS_SENDING, attempts=F('attempts') + 1, last_attempt_at=now)
    )
    if not claimed:
        return None

    # Requery to ensure our object hasn't been picked up elsewhere.
    # Work with the queryset to avoid calling .save() on an individual object.
    in_flight = InvoiceDelivery.objects.filter(
        pk=delivery_id, status=InvoiceDelivery.STATUS_SENDING, last_attempt_at=now
    )
    delivery = in_flight.select_related('invoice').first()
    if delivery is None:  # re-claimed since our claim; not ours to send
        return None

    if not delivery.email:
        in_flight.update(status=InvoiceDelivery.STATUS_FAILED, last_error=MISSING_EMAIL_ERROR)
        logger.warning('Invoice delivery %s has no recipient address', delivery_id)
        return InvoiceDelivery.STATUS_FAILED

    try:
        build_invoice_email(delivery.invoice, delivery.email).send(fail_silently=False)
    except Exception as exc:  # SMTP errors, timeouts, connection refused etc.
        in_flight.update(
            status=InvoiceDelivery.STATUS_FAILED,
            last_error=f'{type(exc).__name__}: {exc}'[:2000],
        )
        logger.warning('Invoice delivery %s failed: %s', delivery_id, exc)
        return InvoiceDelivery.STATUS_FAILED

    in_flight.update(status=InvoiceDelivery.STATUS_SENT, sent_at=timezone.now(), last_error='')
    return InvoiceDelivery.STATUS_SENT


def deliver_invoices(invoice_ids: Iterable[int]) -> None:
    """First delivery attempt for freshly issued invoices.

    Runs after the billing run's transaction commits. It stops at the first failed send to avoid waiting
    an unreasonable amount of time.

    A blank recipient address doesn't stop the batch: that failure is about
    one recipient, not the mail server.
    """
    rows = InvoiceDelivery.objects.filter(
        invoice_id__in=invoice_ids, status=InvoiceDelivery.STATUS_PENDING
    ).values_list('pk', 'email')
    for delivery_id, email in list(rows):
        result = attempt_delivery(delivery_id)
        if result == InvoiceDelivery.STATUS_FAILED and email:
            # Assume that a failure means an issue with the mail server, so wait for a future run.
            logger.warning(
                'Stopping post-run delivery after a send failure; '
                'remaining deliveries are left pending for send_invoice_emails.'
            )
            break


@dataclass
class RetrySummary:
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    exhausted: int = 0
    failed_ids: list[int] = field(default_factory=list)


def retry_deliveries() -> RetrySummary:
    """Retry every delivery that is pending, failed (with attempts left) or
    stuck in `sending`. Rows that have used all attempts stay `failed` for
    manual follow-up."""
    now = timezone.now()
    summary = RetrySummary()
    # A sender that died while holding a row on its last attempt would leave it in `sending` forever;
    # record it as failed so it is reported below. Not strictly necessary, but useful for traceability.
    InvoiceDelivery.objects.filter(
        status=InvoiceDelivery.STATUS_SENDING,
        last_attempt_at__lt=now - STALE_SENDING_AFTER,
        attempts__gte=MAX_ATTEMPTS,
    ).update(status=InvoiceDelivery.STATUS_FAILED, last_error=ABANDONED_ERROR)

    ids = list(
        InvoiceDelivery.objects.filter(_claimable(now)).order_by('pk').values_list('pk', flat=True)
    )
    for delivery_id in ids:
        result = attempt_delivery(delivery_id)
        if result == InvoiceDelivery.STATUS_SENT:
            summary.sent += 1
        elif result == InvoiceDelivery.STATUS_FAILED:
            summary.failed += 1
            summary.failed_ids.append(delivery_id)
        else:
            summary.skipped += 1
    summary.exhausted = InvoiceDelivery.objects.filter(
        status=InvoiceDelivery.STATUS_FAILED, attempts__gte=MAX_ATTEMPTS
    ).count()
    return summary


# --- Rendering ---------------------------------------------------------------

def _gbp(amount: Decimal) -> str:
    return f'£{amount:,.2f}'


def build_invoice_email(invoice: Invoice, to_address: str) -> EmailMessage:
    """
    One plain-text email to one recipient. Content comes from the invoice's
    snapshots, so a retry will be the same.

    In real life this would be more complicated, have some templating, generate PDFs etc. so keeping this basic
    """
    period = f'{invoice.period:%B %Y}'
    lines = list(invoice.lines.all())
    rows = [
        f'  {line.date:%a %d %b %Y}   {line.hours:>6}h   {_gbp(line.daily_rate):>10}/day   {_gbp(line.amount):>12}'
        for line in lines
    ]
    body = '\n'.join([
        f'Invoice {invoice.reference}',
        '',
        f'Bill to:   {invoice.company_name} <{invoice.company_billing_email}>',
        f'Supplier:  {invoice.freelancer_name}',
        f'Period:    {period}',
        f'Issued:    {invoice.created_at:%d %b %Y}',
        '',
        'Work performed (billed per 8-hour day; partial days pro rata):',
        '',
        f'  {"Date":<15}   {"Hours":>7}   {"Day rate":>14}   {"Amount":>12}',
        *rows,
        '',
        f'  Total due: {_gbp(invoice.total)} (GBP)',
        '',
        f'Please quote {invoice.reference} on any correspondence about this invoice.',
    ])
    return EmailMessage(
        subject=f'Invoice {invoice.reference} — {invoice.freelancer_name} for {invoice.company_name}, {period}',
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[to_address],
    )
