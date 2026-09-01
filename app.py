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
    {
        "id": "customer-clearing",
        "topic": "Accounts Receivable",
        "module": "FI",
        "keywords": ["customer", "accounts receivable", "ar", "incoming payment", "clearing", "f-28", "fb70", "dunning"],
        "summary": "Accounts Receivable manages customer invoices, incoming payments, open-item clearing, credit management inputs, dunning, and reconciliation with the general ledger.",
        "steps": [
            "Confirm customer master and company-code data, reconciliation account, and payment terms.",
            "Post or locate the customer invoice and verify its open-item status.",
            "Enter the incoming payment with bank account, value date, amount, and customer.",
            "Select matching open items and account for discounts, residual items, or differences according to policy.",
            "Post the clearing document and review customer and bank G/L balances.",
        ],
        "transactions": ["FB70", "FBL5N", "F-28", "F150"],
        "source": "Curated SAP FI knowledge: Accounts Receivable",
    },
    {
        "id": "asset-accounting",
        "topic": "Asset Accounting",
        "module": "FI-AA",
        "keywords": ["asset", "depreciation", "capitalization", "retirement", "as01", "afab", "asset accounting"],
        "summary": "Asset Accounting tracks fixed assets from acquisition through depreciation, transfer, retirement, and reporting, integrated with the General Ledger.",
        "steps": [
            "Confirm the chart of depreciation, depreciation areas, account determination, and asset class.",
            "Create or validate the asset master, including capitalization date and useful life.",
            "Post the acquisition with the correct transaction type and account assignment.",
            "Execute depreciation in test mode, investigate errors, then run the productive posting.",
            "Reconcile asset values with the general ledger and review the asset history sheet.",
        ],
        "transactions": ["AS01", "AW01N", "ABZON", "AFAB"],
        "source": "Curated SAP FI-AA knowledge: Asset lifecycle",
    },
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database() -> None:
    with closing(connect()) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                topic TEXT NOT NULL,
                module TEXT NOT NULL,
                product TEXT NOT NULL,
                release_name TEXT NOT NULL,
                country TEXT NOT NULL,
                confidence INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                rating TEXT NOT NULL CHECK (rating IN ('helpful', 'not_helpful')),
                comment TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(conversation_id),
                FOREIGN KEY(conversation_id) REFERENCES conversations(id)
            );
            """
        )


def normalize(value: str) -> list[str]:
    return re.findall(r"[a-z0-9./-]+", value.lower())


def find_knowledge(question: str, requested_module: str) -> tuple[dict | None, int]:
    question_lower = question.lower()
    tokens = set(normalize(question))
    best_item = None
    best_score = 0
    for item in KNOWLEDGE:
        score = 0
        for keyword in item["keywords"]:
            keyword_tokens = set(normalize(keyword))
            phrase_pattern = rf"(?<![a-z0-9]){re.escape(keyword.lower())}(?![a-z0-9])"
            if re.search(phrase_pattern, question_lower):
                score += 5 + len(keyword_tokens)
            else:
                score += len(tokens.intersection(keyword_tokens))
        if requested_module != "All" and requested_module in item["module"]:
            score += 2
        if score > best_score:
            best_item, best_score = item, score
    return (best_item, best_score) if best_score >= 2 else (None, best_score)


def create_answer(question: str, context: dict) -> dict:
    item, score = find_knowledge(question, context["module"])
    if not item:
        return {
            "matched": False,
            "topic": "Needs clarification",
            "module": context["module"],
            "confidence": 20,
            "answer": "I could not find enough trusted local knowledge to answer this safely. Add the business process, transaction code or Fiori app, error message, expected result, and whether the issue occurs in ECC or S/4HANA.",
            "steps": [],
            "transactions": [],
            "source": "No sufficiently relevant local source",
            "notice": "No answer was guessed. A SAP FICO specialist should review uncommon or configuration-specific issues.",
            "followups": [
                "Which transaction code or app are you using?",
                "What exact error message or business result are you seeing?",
                "Is this happening in ECC or S/4HANA?",
            ],
        }

    product_note = (
        f"This guidance is framed for {context['product']} {context['release']}. "
        "Transaction availability and application names can differ by release and activated scope."
    )
    country_note = (
        f" Country context: {context['country']}; confirm local tax and statutory requirements."
        if context["country"] != "Global"
        else ""
    )
    confidence = min(94, 58 + score * 4)
    return {
        "matched": True,
        "topic": item["topic"],
        "module": item["module"],
        "confidence": confidence,
        "answer": f"{item['summary']} {product_note}{country_note}",
        "steps": item["steps"],
        "transactions": item["transactions"],
        "source": item["source"],
        "notice": "Validate configuration and test in a non-production system before applying changes.",
        "followups": [
            f"Do you want a step-by-step walkthrough for {item['topic']}?",
            "Should I explain the common validation checks and errors?",
            f"Would you like the key SAP transactions for {item['topic']}?",
        ],
    }


def validate_question(payload: dict) -> tuple[dict | None, list[str]]:
    errors = []
    question = str(payload.get("question", "")).strip()
    if len(question) < 8:
        errors.append("Question must contain at least 8 characters.")
    if len(question) > 1000:
        errors.append("Question must not exceed 1,000 characters.")

    allowed = {
        "module": {"All", "FI", "CO", "FI/CO"},
        "product": {"SAP S/4HANA", "SAP ECC"},
        "release": {"Current", "2023", "2022", "1909", "ECC 6.0"},
        "country": {"Global", "India", "United States", "United Kingdom", "Germany"},
    }
    context = {"question": question}
    for field, choices in allowed.items():
        value = str(payload.get(field, "")).strip()
        if value not in choices:
            errors.append(f"Select a valid {field}.")
        context[field] = value
    if context["product"] == "SAP ECC" and context["release"] != "ECC 6.0":
        errors.append("SAP ECC questions must use the ECC 6.0 release context.")
    if context["product"] == "SAP S/4HANA" and context["release"] == "ECC 6.0":
        errors.append("SAP S/4HANA cannot use the ECC 6.0 release context.")
    return (context if not errors else None), errors


class ApplicationHandler(BaseHTTPRequestHandler):
    server_version = "SAPFICOAssistant/1.0"

    def log_message(self, format: str, *args: object) -> None:
        print(f"[{utc_now()}] {self.address_string()} {format % args}")

    def send_json(self, payload: object, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ValueError("Request body is empty or too large.")
        try:
            data = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("Request body must be valid JSON.") from exc
        if not isinstance(data, dict):
            raise ValueError("Request body must be a JSON object.")
        return data

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/health":
            self.send_json({"status": "ok", "time": utc_now()})
        elif path == "/api/dashboard":
            with closing(connect()) as db:
                total = db.execute("SELECT COUNT(*) AS count FROM conversations").fetchone()["count"]
                helpful = db.execute("SELECT COUNT(*) AS count FROM feedback WHERE rating = 'helpful'").fetchone()["count"]
                topics = [dict(row) for row in db.execute(
                    "SELECT topic, COUNT(*) AS count FROM conversations GROUP BY topic ORDER BY count DESC, topic LIMIT 5"
                )]
            self.send_json({"questions": total, "helpful": helpful, "knowledgeTopics": len(KNOWLEDGE), "topics": topics})

],