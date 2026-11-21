"""Invoice email delivery: content, failure recording, the retry command and
the claim that prevents double sends.
"""
import datetime
import smtplib
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.mail import EmailMessage
from django.core.management import call_command
from django.db import transaction
from django.utils import timezone

from billing.delivery import (
    ABANDONED_ERROR,
    MAX_ATTEMPTS,
    MISSING_EMAIL_ERROR,
    STALE_SENDING_AFTER,
    attempt_delivery,
    deliver_invoices,
)
from billing.models import Invoice, InvoiceDelivery
from billing.services import run_billing

from .conftest import MARCH

pytestmark = pytest.mark.django_db

COMPANY_EMAIL = 'billing@northstar.test'
FREELANCER_EMAIL = 'freelancer@test.test'


@pytest.fixture
def march_entries(northstar, freelancer_user, make_contract, make_entry):
    contract = make_contract(northstar, freelancer_user.freelancer, rate='450.00')
    make_entry(contract, '2026-03-02', hours='7.5')  # 7.5 / 8 * 450 = 421.875 -> £421.88
    make_entry(contract, '2026-03-03', hours='8.0')  # £450.00


@pytest.fixture
def bill_march(northstar, admin_user, march_entries):
    """Run March billing for NorthStar and return the single new invoice."""
    def _bill():
        run = run_billing(company=northstar, triggered_by=admin_user, period=MARCH)
        return run.invoices.get()
    return _bill


@pytest.fixture
def invoice(bill_march):
    return bill_march()


def delivery(invoice, recipient_type):
    return InvoiceDelivery.objects.get(invoice=invoice, recipient_type=recipient_type)


def company_delivery(invoice):
    return delivery(invoice, InvoiceDelivery.RECIPIENT_COMPANY)


def freelancer_delivery(invoice):
    return delivery(invoice, InvoiceDelivery.RECIPIENT_FREELANCER)


def run_command():
    out = StringIO()
    call_command('send_invoice_emails', stdout=out)
    return out.getvalue()


def send_raising(exc, only_to=None):
    """Patch EmailMessage.send to raise `exc` (for every message, or only for
    messages addressed to `only_to`); other messages are sent normally."""
    real_send = EmailMessage.send

    def send(self, *args, **kwargs):
        if only_to is None or only_to in self.to:
            raise exc
        return real_send(self, *args, **kwargs)

    return patch.object(EmailMessage, 'send', send)


def age_sending(row, age):
    InvoiceDelivery.objects.filter(pk=row.pk).update(
        status=InvoiceDelivery.STATUS_SENDING, attempts=1, last_attempt_at=timezone.now() - age
    )


# --- Content -----------------------------------------------------------------

def test_billing_run_leaves_two_pending_deliveries(invoice, mailoutbox):
    rows = InvoiceDelivery.objects.filter(invoice=invoice)
    assert {(r.recipient_type, r.email, r.status, r.attempts) for r in rows} == {
        ('company', COMPANY_EMAIL, 'pending', 0),
        ('freelancer', FREELANCER_EMAIL, 'pending', 0),
    }
    assert mailoutbox == []


def test_deliver_invoices_sends_one_email_per_recipient(invoice, mailoutbox):
    deliver_invoices([invoice.pk])

    assert sorted(m.to for m in mailoutbox) == [[COMPANY_EMAIL], [FREELANCER_EMAIL]]
    for message in mailoutbox:
        assert invoice.reference in message.subject
        body = message.body
        assert 'Mon 02 Mar 2026' in body and '7.50h' in body and '£421.88' in body
        assert 'Tue 03 Mar 2026' in body and '8.00h' in body
        assert body.count('£450.00/day') == 2
        assert 'Total due: £871.88 (GBP)' in body

    for row in InvoiceDelivery.objects.filter(invoice=invoice):
        assert row.status == InvoiceDelivery.STATUS_SENT
        assert row.sent_at is not None
        assert row.attempts == 1
        assert row.last_error == ''


# --- Failure -----------------------------------------------------------------

@pytest.mark.parametrize('exc', [
    smtplib.SMTPException('mail server said no'),
    ConnectionRefusedError(61, 'Connection refused'),
])
def test_send_failure_is_recorded_and_invoice_untouched(invoice, mailoutbox, exc):
    before = Invoice.objects.filter(pk=invoice.pk).values().get()

    with send_raising(exc):
        deliver_invoices([invoice.pk])  # must not raise

    failed = company_delivery(invoice)  # attempted first
    assert failed.status == InvoiceDelivery.STATUS_FAILED
    assert type(exc).__name__ in failed.last_error
    assert str(exc) in failed.last_error
    assert failed.attempts == 1
    assert failed.last_attempt_at is not None
    assert failed.sent_at is None
    assert Invoice.objects.filter(pk=invoice.pk).values().get() == before
    assert invoice.lines.count() == 2
    assert mailoutbox == []


def test_blank_freelancer_email_fails_that_delivery_only(freelancer_user, bill_march, mailoutbox):
    freelancer_user.email = ''
    freelancer_user.save()
    invoice = bill_march()

    deliver_invoices([invoice.pk])

    blank = freelancer_delivery(invoice)
    assert blank.status == InvoiceDelivery.STATUS_FAILED
    assert blank.last_error == MISSING_EMAIL_ERROR
    assert blank.attempts == 1
    assert company_delivery(invoice).status == InvoiceDelivery.STATUS_SENT
    assert [m.to for m in mailoutbox] == [[COMPANY_EMAIL]]


@pytest.fixture
def two_invoices(northstar, admin_user, march_entries, second_freelancer, make_contract, make_entry):
    make_entry(make_contract(northstar, second_freelancer), '2026-03-02')
    run = run_billing(company=northstar, triggered_by=admin_user, period=MARCH)
    invoices = list(run.invoices.order_by('pk'))
    assert len(invoices) == 2
    return invoices


def test_post_run_delivery_stops_at_the_first_send_failure(two_invoices, mailoutbox):
    """With SMTP down, the post-run attempt costs one send (one EMAIL_TIMEOUT
    at most), not one per delivery."""
    send = patch.object(EmailMessage, 'send', side_effect=ConnectionRefusedError(61, 'refused'))
    with send as mock_send:
        deliver_invoices([invoice.pk for invoice in two_invoices])

    assert mock_send.call_count == 1
    rows = list(InvoiceDelivery.objects.filter(invoice__in=two_invoices))  # attempt order
    assert [(r.status, r.attempts) for r in rows] == [
        ('failed', 1), ('pending', 0), ('pending', 0), ('pending', 0),
    ]
    assert 'ConnectionRefusedError' in rows[0].last_error

    # SMTP is back: the command sends everything that is left.
    assert run_command().strip() == 'Sent 4, failed 0, skipped 0.'
    assert len(mailoutbox) == 4


def test_blank_email_does_not_stop_post_run_delivery(two_invoices, mailoutbox):
    # Blank the address on the first invoice's freelancer delivery, which is
    # attempted before the second invoice's deliveries.
    first, second = two_invoices
    InvoiceDelivery.objects.filter(
        invoice=first, recipient_type=InvoiceDelivery.RECIPIENT_FREELANCER
    ).update(email='')

    deliver_invoices([first.pk, second.pk])

    assert freelancer_delivery(first).last_error == MISSING_EMAIL_ERROR
    assert company_delivery(second).status == InvoiceDelivery.STATUS_SENT
    assert freelancer_delivery(second).status == InvoiceDelivery.STATUS_SENT
    assert len(mailoutbox) == 3


# --- Retry command -----------------------------------------------------------

def test_command_sends_pending_deliveries(invoice, mailoutbox):
    output = run_command()

    assert output.strip() == 'Sent 2, failed 0, skipped 0.'
    assert len(mailoutbox) == 2
    assert set(
        InvoiceDelivery.objects.filter(invoice=invoice).values_list('status', flat=True)
    ) == {InvoiceDelivery.STATUS_SENT}


def test_command_resends_only_the_failed_recipient(invoice, mailoutbox):
    with send_raising(smtplib.SMTPException('rejected'), only_to=FREELANCER_EMAIL):
        deliver_invoices([invoice.pk])
    assert [m.to for m in mailoutbox] == [[COMPANY_EMAIL]]
    assert freelancer_delivery(invoice).status == InvoiceDelivery.STATUS_FAILED
    mailoutbox.clear()

    output = run_command()

    assert output.strip() == 'Sent 1, failed 0, skipped 0.'
    assert [m.to for m in mailoutbox] == [[FREELANCER_EMAIL]]
    retried, untouched = freelancer_delivery(invoice), company_delivery(invoice)
    assert (retried.status, retried.attempts, retried.last_error) == ('sent', 2, '')
    assert (untouched.status, untouched.attempts) == ('sent', 1)


def test_command_stops_retrying_after_max_attempts(invoice, mailoutbox):
    with send_raising(smtplib.SMTPException('down')):
        for _ in range(MAX_ATTEMPTS):
            output = run_command()

    rows = list(InvoiceDelivery.objects.filter(invoice=invoice))
    assert [(r.status, r.attempts) for r in rows] == [('failed', MAX_ATTEMPTS)] * 2
    # The final attempt reports the failures, their ids and that they are now exhausted.
    assert output.splitlines() == [
        'Sent 0, failed 2, skipped 0. 2 deliveries have exhausted 5 attempts '
        'and need manual follow-up.',
        f'Failed delivery ids: {rows[0].pk}, {rows[1].pk}',
    ]

    # With sending working again, exhausted rows are still left alone.
    output = run_command()

    assert mailoutbox == []
    assert [(r.status, r.attempts) for r in InvoiceDelivery.objects.filter(invoice=invoice)] == [
        ('failed', MAX_ATTEMPTS)
    ] * 2
    assert output.strip() == (
        'Sent 0, failed 0, skipped 0. 2 deliveries have exhausted 5 attempts '
        'and need manual follow-up.'
    )


# --- No double send ----------------------------------------------------------

def test_sent_delivery_is_not_sent_again(invoice, mailoutbox):
    deliver_invoices([invoice.pk])
    row = company_delivery(invoice)
    mailoutbox.clear()

    assert attempt_delivery(row.pk) is None
    assert run_command().strip() == 'Sent 0, failed 0, skipped 0.'
    assert mailoutbox == []
    row.refresh_from_db()
    assert (row.status, row.attempts) == ('sent', 1)


def test_concurrent_attempt_during_send_is_refused(invoice, mailoutbox):
    """A second sender arriving while the first is mid-send (row `sending`)
    can't claim the row."""
    row = company_delivery(invoice)
    real_send = EmailMessage.send
    racing_results = []

    def send(self, *args, **kwargs):
        racing_results.append(attempt_delivery(row.pk))
        return real_send(self, *args, **kwargs)

    with patch.object(EmailMessage, 'send', send):
        assert attempt_delivery(row.pk) == InvoiceDelivery.STATUS_SENT

    assert racing_results == [None]
    assert len(mailoutbox) == 1
    row.refresh_from_db()
    assert (row.status, row.attempts) == ('sent', 1)


def test_recently_claimed_sending_row_is_skipped(invoice, mailoutbox):
    row = company_delivery(invoice)
    age_sending(row, datetime.timedelta(minutes=1))

    assert attempt_delivery(row.pk) is None
    run_command()  # sends the pending freelancer row only

    assert [m.to for m in mailoutbox] == [[FREELANCER_EMAIL]]
    row.refresh_from_db()
    assert (row.status, row.attempts) == ('sending', 1)


def test_stale_sending_row_is_reclaimed_and_sent(invoice, mailoutbox):
    row = company_delivery(invoice)
    age_sending(row, STALE_SENDING_AFTER + datetime.timedelta(seconds=1))

    assert attempt_delivery(row.pk) == InvoiceDelivery.STATUS_SENT
    assert [m.to for m in mailoutbox] == [[COMPANY_EMAIL]]
    row.refresh_from_db()
    assert (row.status, row.attempts) == ('sent', 2)


@pytest.mark.parametrize('send_raises', [False, True], ids=['sent', 'failed'])
def test_outcome_of_a_superseded_claim_is_not_recorded(invoice, send_raises):
    """A sender whose claim went stale and was re-claimed by another sender
    must not overwrite the newer claimant's row when it finally finishes."""
    row = company_delivery(invoice)
    reclaimed_at = timezone.now() + datetime.timedelta(minutes=11)

    def send(self, *args, **kwargs):
        # While we are mid-send, another sender takes the row over as stale.
        InvoiceDelivery.objects.filter(pk=row.pk).update(
            attempts=2, last_attempt_at=reclaimed_at
        )
        if send_raises:
            raise smtplib.SMTPException('late failure')

    with patch.object(EmailMessage, 'send', send):
        attempt_delivery(row.pk)

    row.refresh_from_db()
    # Still exactly as the newer claimant left it.
    assert (row.status, row.attempts, row.last_attempt_at) == ('sending', 2, reclaimed_at)
    assert (row.last_error, row.sent_at) == ('', None)


def test_abandoned_row_on_last_attempt_is_marked_failed_and_reported(invoice, mailoutbox):
    # A sender died while holding the row on its final attempt.
    row = company_delivery(invoice)
    age_sending(row, STALE_SENDING_AFTER + datetime.timedelta(seconds=1))
    InvoiceDelivery.objects.filter(pk=row.pk).update(attempts=MAX_ATTEMPTS)

    output = run_command()

    row.refresh_from_db()
    assert (row.status, row.attempts) == ('failed', MAX_ATTEMPTS)
    assert row.last_error == ABANDONED_ERROR
    assert '1 deliveries have exhausted 5 attempts' in output
    assert [m.to for m in mailoutbox] == [[FREELANCER_EMAIL]]  # only the pending row


# --- Transaction boundary ----------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_no_email_is_sent_for_a_rolled_back_run(northstar, admin_user, march_entries, mailoutbox):
    """Sending after a commit is covered by the API email test."""
    with pytest.raises(RuntimeError), transaction.atomic():
        run_billing(company=northstar, triggered_by=admin_user, period=MARCH)
        assert mailoutbox == []  # not sent from inside the transaction
        raise RuntimeError('rollback')

    # The post-commit send is discarded with the invoice.
    assert not Invoice.objects.exists()
    assert mailoutbox == []


@pytest.mark.django_db(transaction=True)
def test_crash_in_post_commit_send_keeps_the_invoice(northstar, admin_user, march_entries, mailoutbox):
    with patch('billing.delivery.attempt_delivery', side_effect=RuntimeError('db went away')):
        run = run_billing(company=northstar, triggered_by=admin_user, period=MARCH)

    # The run still succeeded; its deliveries wait in the outbox for the command.
    invoice = run.invoices.get()
    assert set(invoice.deliveries.values_list('status', flat=True)) == {InvoiceDelivery.STATUS_PENDING}
    assert mailoutbox == []

    assert run_command().strip() == 'Sent 2, failed 0, skipped 0.'
    assert len(mailoutbox) == 2
