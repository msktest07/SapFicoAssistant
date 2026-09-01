# SAP FICO Assistant

A dependency-free Python, HTML, CSS, and JavaScript application that answers common SAP FI/CO questions from curated local knowledge. It includes validated SAP context, persistent history, feedback, topic browsing, and a usage dashboard.

## Run locally

Requires Python 3.10 or newer.

```powershell
python app.py
```

Open <http://127.0.0.1:8000>. The SQLite database (`sap_fico.db`) is created automatically and is excluded from version control.

## Test

```powershell
python -m unittest discover -s tests -v
```

This version is a safe local prototype. It does not connect to SAP or a hosted language model and must not be treated as authoritative financial, tax, or production configuration advice.
