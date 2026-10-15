# GST FAQ Chatbot (No AI model)

A local Windows web chatbot that searches `faq_57th_gstc.pdf` using PostgreSQL full-text search plus a small deterministic feature-hashing vector stored with pgvector. It does **not** call an AI model or cloud API and does not generate new prose; it returns the matching PDF excerpt and page number.

> The hash vector is not a semantic embedding. It is a lightweight lexical similarity signal. PostgreSQL full-text search is the main retrieval method.

## Requirements
- Windows 10/11
- Python 3.10+
- PostgreSQL
- pgvector extension installed for your PostgreSQL version

## 1. Install PostgreSQL and pgvector
Install PostgreSQL. Install a matching pgvector build for your PostgreSQL version (or use a PostgreSQL distribution that bundles pgvector). Verify that the extension is available to the server. In `psql`, run:
```sql
CREATE DATABASE gst_faq;
\c gst_faq
CREATE EXTENSION IF NOT EXISTS vector;
```
If `CREATE EXTENSION vector` says the extension files are missing, install pgvector on the PostgreSQL server first; pip installing the Python package alone is not enough.

## 2. Create a Python environment
Open PowerShell in this folder:
```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```
If PowerShell blocks activation, either run `Set-ExecutionPolicy -Scope Process Bypass` for this PowerShell window, or use:
```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 3. Configure database connection
Copy `.env.example` to `.env`, then edit `DATABASE_URL` with your PostgreSQL username, password, host and database:
```text
DATABASE_URL=postgresql://postgres:YOUR_PASSWORD@localhost:5432/gst_faq
FLASK_DEBUG=1
```
URL-encode special characters in your password (for example `@` becomes `%40`).

## 4. Create the table
Connect to the `gst_faq` database, then run:
```powershell
psql -U postgres -d gst_faq -f schema.sql
```
Or paste the contents of `schema.sql` into pgAdmin Query Tool while connected to `gst_faq`. The DB user needs permission to create the extension/table/indexes.

## 5. Extract and index PDF text
The PDF is included in this folder. From PowerShell:
```powershell
python ingest.py
```
This extracts text page-by-page, splits long pages into chunks, and stores each chunk with its PDF page number and a deterministic 256-dimensional hash vector. Re-running ingestion replaces the rows from this PDF.

## 6. Start the website
```powershell
python app.py
```
Open http://127.0.0.1:5000 in your browser.

## Example questions
- How can I apply for GST registration?
- How do I cancel GST registration?
- What is the simplified registration scheme for ECO sellers?
- How are refunds processed?

## How search works
1. Convert the question into PostgreSQL full-text query terms.
2. Rank matching text with `ts_rank_cd`.
3. Use the deterministic hash vector as a secondary rough lexical similarity signal.
4. Return the matched PDF text and page number, with additional candidate excerpts expandable in the UI.

## Troubleshooting
- `extension "vector" is not available`: install the server-side pgvector extension for your PostgreSQL build, then restart PostgreSQL.
- `password authentication failed`: correct the `.env` connection string.
- No chunks indexed: confirm the PDF is beside `app.py`, then run `python ingest.py`.
- PDF text extraction may miss content embedded only as scanned images. This PDF appears to have selectable/extractable text on its FAQ pages, but review low-quality pages manually.
- If the server is not running, the browser cannot connect.

## Important
This is a retrieval demo, not legal advice. The PDF itself contains GST Council recommendations and references to future notifications; verify current legal status with official GST/CBIC sources before acting.
