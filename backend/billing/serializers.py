from collections.abc import Iterable

from rest_framework import serializers

from billing.models import BillingRun, Invoice, InvoiceDelivery, InvoiceLine

CURRENCY = 'GBP'
RUN_STATUS_COMPLETED = 'completed'


def aggregate_delivery_status(deliveries: Iterable[InvoiceDelivery]) -> str:
    """One status for an invoice's deliveries.

    'failed' if any delivery failed, 'sent' once every delivery has been sent,
    otherwise 'pending' (which includes rows currently being sent).
    """
    statuses = {delivery.status for delivery in deliveries}
    if InvoiceDelivery.STATUS_FAILED in statuses:
        return InvoiceDelivery.STATUS_FAILED
    if statuses == {InvoiceDelivery.STATUS_SENT}:
        return InvoiceDelivery.STATUS_SENT
    return InvoiceDelivery.STATUS_PENDING


class BillingRunSerializer(serializers.ModelSerializer):
    """Response body for POST /api/billing/runs/."""

    run_id = serializers.IntegerField(source='pk', read_only=True)
    status = serializers.SerializerMethodField()

    class Meta:
        model = BillingRun
        fields = ['run_id', 'invoices_generated', 'invoices_existing', 'status']
        read_only_fields = fields

    def get_status(self, run: BillingRun) -> str:
        # Runs are synchronous: by the time a run is serialised it has completed, so always return that.
        return RUN_STATUS_COMPLETED


class InvoiceSummarySerializer(serializers.ModelSerializer):
    period = serializers.DateField(format='%Y-%m', read_only=True)
    currency = serializers.SerializerMethodField()
    delivery_status = serializers.SerializerMethodField()

    class Meta:
        model = Invoice
        fields = [
            'id', 'reference', 'freelancer', 'freelancer_name', 'company_name', 'period',
            'currency', 'total', 'created_at', 'delivery_status',
        ]
        read_only_fields = fields

    def get_currency(self, invoice: Invoice) -> str:
        # Not really necessary as contract does not define a currency, but in real life we'd want this.
        return CURRENCY

    def get_delivery_status(self, invoice: Invoice) -> str:
        # Return state of the batch of emails.
        return aggregate_delivery_status(invoice.deliveries.all())


class InvoiceLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoiceLine
        fields = ['id', 'date', 'hours', 'daily_rate', 'amount', 'contract', 'timesheet_entry']
        read_only_fields = fields


class InvoiceDeliverySerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoiceDelivery
        fields = [
            'recipient_type', 'email', 'status', 'attempts', 'last_error', 'last_attempt_at',
            'sent_at',
        ]
        read_only_fields = fields


class InvoiceDetailSerializer(InvoiceSummarySerializer):
    lines = InvoiceLineSerializer(many=True, read_only=True)
    deliveries = InvoiceDeliverySerializer(many=True, read_only=True)

    class Meta(InvoiceSummarySerializer.Meta):
        fields = InvoiceSummarySerializer.Meta.fields + [
            'billing_run', 'company_billing_email', 'lines', 'deliveries',
        ]
        read_only_fields = fields
