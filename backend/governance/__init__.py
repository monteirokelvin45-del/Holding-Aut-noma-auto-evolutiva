"""Diretoria da holding: CEO, RH, contabilidade e jurídico."""

from .accountant_auditor import AccountantAuditor, accountant
from .hr_department import HRDepartment, hr_department
from .legal_compliance import LegalCompliance, legal_department

__all__ = [
    "AccountantAuditor",
    "accountant",
    "HRDepartment",
    "hr_department",
    "LegalCompliance",
    "legal_department",
]
