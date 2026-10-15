
import os
import re
import hashlib
from pathlib import Path
from difflib import SequenceMatcher

import numpy as np
import psycopg2
from psycopg2.extras import RealDictCursor
from pgvector.psycopg2 import register_vector
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from pypdf import PdfReader

load_dotenv()

app = Flask(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")
PDF_PATH = Path(__file__).resolve().parent / "faq_57th_gstc.pdf"
SOURCE_NAME = PDF_PATH.name
VECTOR_DIM = 256


def connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing.")

    conn = psycopg2.connect(
        DATABASE_URL,
        connect_timeout=10,
        options="-c statement_timeout=15000"
    )
    register_vector(conn)
    return conn


def text_to_vector(text):
    """Deterministic hash vectors; no AI model or AI embeddings."""
    tokens = re.findall(r"[a-zA-Z0-9₹]+", text.lower())

    vec = np.zeros(VECTOR_DIM, dtype=np.float32)
    features = tokens + [
        tokens[i] + "_" + tokens[i + 1]
        for i in range(len(tokens) - 1)
    ]

    for feature in features:
        digest = hashlib.blake2b(
            feature.encode("utf-8"),
            digest_size=8
        ).digest()

        idx = int.from_bytes(digest[:4], "little") % VECTOR_DIM
        sign = 1.0 if digest[4] & 1 else -1.0
        vec[idx] += sign

    norm = np.linalg.norm(vec)
    if norm:
        vec /= norm

    return vec



def split_text(text, max_chars=5000, overlap=300):
    text = re.sub(r"\r\n?", "\n", text or "").strip()

    if not text:
        return []

    paragraphs = [
        re.sub(r"[ \t]+", " ", p).strip()
        for p in re.split(r"\n\s*\n", text)
        if p.strip()
    ]

    chunks = []
    current = ""

    for paragraph in paragraphs:
        pieces = re.split(r"(?<=[.!?])\s+", paragraph)

        for piece in pieces:
            if len(piece) > max_chars:
                if current:
                    chunks.append(current)
                    current = ""

                start = 0
                while start < len(piece):
                    end = min(start + max_chars, len(piece))
                    chunks.append(piece[start:end].strip())

                    if end == len(piece):
                        break

                    start = end - overlap
                continue

            candidate = (
                f"{current}\n\n{piece}".strip()
                if current else piece
            )

            if len(candidate) <= max_chars:
                current = candidate
            else:
                tail = current[-overlap:] if current and overlap else ""
                chunks.append(current)
                current = f"{tail}\n\n{piece}".strip() if tail else piece

    if current:
        chunks.append(current)

    return chunks



def ensure_schema():
    schema_path = Path(__file__).resolve().parent / "schema.sql"
    schema = schema_path.read_text(encoding="utf-8")

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(schema)


def ingest_pdf():
    if not PDF_PATH.exists():
        raise FileNotFoundError(
            f"PDF not found: {PDF_PATH}. "
            "Put faq_57th_gstc.pdf beside app.py."
        )

    reader = PdfReader(str(PDF_PATH))
    rows = []

    for page_num, page in enumerate(reader.pages, start=1):
        page_text = page.extract_text() or ""

        for chunk_num, chunk in enumerate(
            split_text(page_text), start=1
        ):
            rows.append((
                SOURCE_NAME,
                page_num,
                chunk_num,
                chunk,
                text_to_vector(chunk)
            ))

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM faq_chunks WHERE source_file = %s",
                (SOURCE_NAME,)
            )

            for source, page, chunk_num, content, embedding in rows:
                cur.execute(
                    """
                    INSERT INTO faq_chunks
                        (source_file, page_number, chunk_number,
                         content, embedding)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (source, page, chunk_num, content, embedding)
                )

    return {"pages": len(reader.pages), "chunks": len(rows)}


def normalize(text):
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9₹]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_faqs(content):
    """
    Extract numbered FAQ entries from a chunk.

    Each entry contains a question and its following answer.
    The answer ends when the next numbered question is found.
    """
    text = re.sub(r"\s+", " ", content or "").strip()

    text = re.sub(
        r"Page\s+\d+\s+of\s+\d+",
        " ",
        text,
        flags=re.IGNORECASE
    )

    # Find numbered entries whose question ends in a question mark.
    pattern = re.compile(
        r"(?<!\S)(\d{1,3})\.\s+"
        r"(.{3,250}?\?)"
    )

    matches = list(pattern.finditer(text))
    faqs = []

    for i, match in enumerate(matches):
        question = match.group(2).strip()
        answer_start = match.end()

        # The answer ends where the next detected FAQ question begins.
        answer_end = (
            matches[i + 1].start()
            if i + 1 < len(matches)
            else len(text)
        )

        answer = text[answer_start:answer_end].strip()

        # Remove leading separators or table fragments.
        answer = re.sub(r"^[\s:|\-]+", "", answer).strip()

        if question and answer:
            faqs.append({
                "question": question,
                "answer": answer
            })

    return faqs


def score_question(user_question, faq_question, faq_answer=""):
    """Lexical similarity only; no generative model."""
    q = normalize(user_question)
    fq = normalize(faq_question)
    ans = normalize(faq_answer)

    if not q or not fq:
        return 0.0

    stop_words = {
        "how", "can", "the", "for", "you", "are", "what",
        "where", "does", "with", "from", "this", "that",
        "have", "has", "was", "who", "why", "when", "which",
        "and", "but", "not", "its", "into", "your"
    }

    query_words = {
        word for word in q.split()
        if len(word) > 2 and word not in stop_words
    }

    if not query_words:
        return SequenceMatcher(None, q, fq).ratio()

    faq_words = set(fq.split())
    answer_words = set(ans.split())

    question_coverage = (
        sum(word in faq_words for word in query_words)
        / len(query_words)
    )

    answer_coverage = (
        sum(word in answer_words for word in query_words)
        / len(query_words)
    )

    similarity = SequenceMatcher(None, q, fq).ratio()

    return (
        0.55 * question_coverage
        + 0.30 * similarity
        + 0.15 * answer_coverage
    )


def search_faq(question, limit=30):
    """
    Search the existing chunks, extract individual FAQs,
    and return the best matching FAQ rather than the whole chunk.
    """
    question = question.strip()

    if not question:
        return None

    qvec = text_to_vector(question)
    rows = []

    with connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Primary search: PostgreSQL full-text + pgvector.
            cur.execute(
                """
                WITH query AS (
                    SELECT websearch_to_tsquery(
                        'english', %s
                    ) AS tsq
                )
                SELECT
                    f.id,
                    f.page_number,
                    f.content,
                    ts_rank_cd(
                        f.search_vector, query.tsq
                    ) AS text_rank,
                    CASE
                        WHEN f.embedding IS NULL THEN 0
                        ELSE 1 - (
                            f.embedding <=> %s::vector
                        )
                    END AS vector_score
                FROM faq_chunks f
                CROSS JOIN query
                WHERE (
                    numnode(query.tsq) > 0
                    AND f.search_vector @@ query.tsq
                )
                OR (
                    f.embedding IS NOT NULL
                    AND (f.embedding <=> %s::vector) < 0.55
                )
                ORDER BY (
                    ts_rank_cd(
                        f.search_vector, query.tsq
                    ) * 0.75
                    + GREATEST(
                        0,
                        CASE
                            WHEN f.embedding IS NULL THEN 0
                            ELSE 1 - (
                                f.embedding <=> %s::vector
                            )
                        END
                    ) * 0.25
                ) DESC
                LIMIT %s
                """,
                (question, qvec, qvec, qvec, limit)
            )

            rows = cur.fetchall()

            # Fallback search if full-text/vector search found nothing.
            if not rows:
                stop_words = {
                    "how", "can", "the", "for", "you", "are",
                    "what", "where", "does", "with", "from",
                    "this", "that", "have", "has", "was"
                }

                words = list(dict.fromkeys(
                    word.lower()
                    for word in re.findall(
                        r"[a-zA-Z0-9]+", question
                    )
                    if len(word) > 2
                    and word.lower() not in stop_words
                ))

                if words:
                    conditions = " OR ".join(
                        ["content ILIKE %s"] * len(words)
                    )

                    params = [f"%{word}%" for word in words]

                    cur.execute(
                        f"""
                        SELECT id, page_number, content,
                               0::float AS text_rank,
                               0::float AS vector_score
                        FROM faq_chunks
                        WHERE {conditions}
                        ORDER BY page_number
                        LIMIT %s
                        """,
                        params + [limit]
                    )

                    rows = cur.fetchall()

    # Extract FAQ-level answers from each matching chunk.
    candidates = []

    for row in rows:
        for faq in extract_faqs(row["content"]):
            score = score_question(
                question,
                faq["question"],
                faq["answer"]
            )

            candidates.append({
                "question": faq["question"],
                "answer": faq["answer"],
                "page": row["page_number"],
                "score": score
            })

    if not candidates:
        return None

    # Deduplicate FAQs that appear in overlapping chunks.
    unique = {}

    for item in candidates:
        key = normalize(item["question"])

        if (
            key not in unique
            or item["score"] > unique[key]["score"]
        ):
            unique[key] = item

    candidates = sorted(
        unique.values(),
        key=lambda item: item["score"],
        reverse=True
    )

    best = candidates[0]

    # Reject weak matches rather than return an unrelated answer.
    if best["score"] < 0.30:
        return None

    return best


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    question = str(data.get("question", "")).strip()

    if not question:
        return jsonify({
            "answer": "Please enter a question.",
            "sources": []
        }), 400

    try:
        result = search_faq(question)

        if not result:
            return jsonify({
                "answer": (
                    "I couldn't find a matching FAQ answer in "
                    "the uploaded GST PDF. Try a more specific question."
                ),
                "sources": []
            })

        # Return ONLY the selected FAQ answer.
        return jsonify({
            "answer": result["answer"],
            "sources": [{
                "file": SOURCE_NAME,
                "page": result["page"],
                "question": result["question"]
            }]
        })

    except Exception:
        app.logger.exception("Chat search failed")

        return jsonify({
            "answer": (
                "The search failed. Check your PostgreSQL "
                "connection and database setup."
            ),
            "sources": []
        }), 500


@app.route("/api/health")
def health():
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM faq_chunks")
                count = cur.fetchone()[0]

        return jsonify({
            "ok": True,
            "indexed_chunks": count
        })

    except Exception as exc:
        app.logger.exception("Health check failed")

        return jsonify({
            "ok": False,
            "error": str(exc)
        }), 500


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=os.getenv("FLASK_DEBUG", "0") == "1"
    )
