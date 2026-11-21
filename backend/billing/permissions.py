from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.views import APIView


class IsCompanyAdmin(BasePermission):
    """Billing is a company-admin-only area, checks that the user is an admin"""

    message = 'Only company admins can access billing.'

    def has_permission(self, request: Request, view: APIView) -> bool:
        return hasattr(request.user, 'company_admin')
