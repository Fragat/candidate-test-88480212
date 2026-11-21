import logging

from django.db import IntegrityError
from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from billing.models import Invoice
from billing.permissions import IsCompanyAdmin
from billing.serializers import (
    BillingRunSerializer,
    InvoiceDetailSerializer,
    InvoiceSummarySerializer,
)
from billing.services import InvalidMonth, parse_billable_month, parse_month, run_billing

logger = logging.getLogger(__name__)

RUN_CONFLICT_DETAIL = (
    'This month could not be billed because one or more of its timesheet entries '
    'are already on another invoice, which can happen if an entry was edited after '
    'it was billed. No changes were made.'
)


class BillingRunView(APIView):
    permission_classes = [IsAuthenticated, IsCompanyAdmin]

    def post(self, request: Request) -> Response:
        # The body is arbitrary JSON: anything but a string month counts as missing.
        month = request.data.get('month') if isinstance(request.data, dict) else None
        if not isinstance(month, str):
            month = None
        try:
            period = parse_billable_month(month)
        except InvalidMonth as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        company = request.user.company_admin.company

        try:
            run = run_billing(company=company, triggered_by=request.user, period=period)
        except IntegrityError:
            # This should only really happen when things have been moved intentionally. E.g. Updating a time sheet entry
            # after it has already been invoiced. This is a stopgap for now.
            logger.exception('Billing run for company %s %s hit a database constraint', company.pk, f'{period:%Y-%m}')
            return Response({'detail': RUN_CONFLICT_DETAIL}, status=status.HTTP_409_CONFLICT)

        return Response(BillingRunSerializer(run).data, status=status.HTTP_201_CREATED)


class InvoiceViewSet(viewsets.ViewSet):
    """Invoices for the admin's company."""

    permission_classes = [IsAuthenticated, IsCompanyAdmin]
    lookup_value_regex = r'\d+'

    def get_queryset(self) -> QuerySet[Invoice]:
        return Invoice.objects.filter(
            company_id=self.request.user.company_admin.company_id
        ).prefetch_related('deliveries')

    def list(self, request: Request) -> Response:
        """GET /api/invoices/?month=YYYY-MM (month optional)."""
        invoices = self.get_queryset()

        month = request.query_params.get('month')
        if month is not None:
            try:
                invoices = invoices.filter(period=parse_month(month))
            except InvalidMonth as exc:
                return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(InvoiceSummarySerializer(invoices, many=True).data)

    def retrieve(self, request: Request, pk: str | None = None) -> Response:
        """GET /api/invoices/<id>/ with lines and deliveries."""
        invoice = get_object_or_404(self.get_queryset().prefetch_related('lines'), pk=pk)
        return Response(InvoiceDetailSerializer(invoice).data)
