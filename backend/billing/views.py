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
    BillingRunRequestSerializer,
    BillingRunSerializer,
    InvoiceDetailSerializer,
    InvoiceListQuerySerializer,
    InvoiceSummarySerializer,
)
from billing.services import run_billing

logger = logging.getLogger(__name__)

RUN_CONFLICT_DETAIL = (
    'This month could not be billed because one or more of its timesheet entries '
    'are already on another invoice, which can happen if an entry was edited after '
    'it was billed. No changes were made.'
)


class BillingRunView(APIView):
    permission_classes = [IsAuthenticated, IsCompanyAdmin]

    def post(self, request: Request) -> Response:
        body = BillingRunRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        period = body.validated_data['month']

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
        query = InvoiceListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)

        invoices = self.get_queryset()
        if 'month' in query.validated_data:
            invoices = invoices.filter(period=query.validated_data['month'])
        return Response(InvoiceSummarySerializer(invoices, many=True).data)

    def retrieve(self, request: Request, pk: str | None = None) -> Response:
        """GET /api/invoices/<id>/ with lines and deliveries."""
        invoice = get_object_or_404(self.get_queryset().prefetch_related('lines'), pk=pk)
        return Response(InvoiceDetailSerializer(invoice).data)
