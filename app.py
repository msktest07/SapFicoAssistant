"""SAP FICO Assistant: dependency-free local web application."""

from __future__ import annotations

import json
import mimetypes
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DATABASE = ROOT / "sap_fico.db"
HOST = "127.0.0.1"
PORT = 8000
MAX_BODY_BYTES = 32_000


KNOWLEDGE = [
    {
        "id": "gl-posting",
        "topic": "General Ledger",
        "module": "FI",
        "keywords": ["general ledger", "gl", "journal", "posting", "fb50", "f-02", "document"],
        "summary": "General Ledger posting records a balanced accounting document using company code, posting date, document type, currency, accounts, amounts, and tax or assignment data where relevant.",
        "steps": [
            "Confirm the posting period is open and the user has authorization for the company code.",
            "Enter the document date, posting date, company code, currency, and document type.",
            "Add debit and credit line items with valid G/L accounts, amounts, cost objects, and tax codes.",
            "Simulate the document, resolve validation messages, and confirm total debits equal total credits.",
            "Post and retain the generated accounting document number for review or reversal.",
        ],
        "transactions": ["FB50", "F-02", "FB03", "FB08"],
        "source": "Curated SAP FI knowledge: General Ledger postings",
    },
    {
        "id": "vendor-payment",
        "topic": "Accounts Payable",
        "module": "FI",
        "keywords": ["vendor", "supplier", "accounts payable", "ap", "invoice", "payment", "f110", "fb60", "miro"],
        "summary": "Accounts Payable manages supplier master data, invoices, credit memos, open items, automatic payments, withholding tax, and reconciliation with the general ledger.",
        "steps": [
            "Validate supplier, company-code data, payment terms, bank details, and reconciliation account.",
            "Post or verify the supplier invoice and confirm tax, baseline date, and payment block.",
            "For automatic payment, maintain payment-method and house-bank configuration and create an F110 proposal.",
            "Review exceptions in the proposal before scheduling the payment run.",
            "Verify clearing documents, payment media, and the related G/L postings.",
        ],
        "transactions": ["FB60", "MIRO", "FBL1N", "F110"],
        "source": "Curated SAP FI knowledge: Accounts Payable",
    },
]
