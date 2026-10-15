import os
import re
import logging
from pathlib import Path
from difflib import SequenceMatcher

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

load_dotenv()
app = Flask(__name__)
app.logger.setLevel(logging.INFO)

DATABASE_URL = os.getenv("DATABASE_URL")
SOURCE_NAME = "faq_57th_gstc.pdf"


def connect():
    """Open a PostgreSQL connection with connection and query time limits."""
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured in Render Environment.")
    return psycopg2.connect(
        DATABASE_URL,
        connect_timeout=10,
        options="-c statement_timeout=10000",
        application_name="gst-faq-chatbot",
    )


def normalize(text):
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9₹]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_faqs(content):
    """Split a text chunk into numbered FAQ question/answer pairs."""
    text = re.sub(r"\s+", " ", content or "").strip()
    text = re.sub(r"Page\s+\d+\s+of\s+\d+", " ", text, flags=re.IGNORECASE)
    pattern = re.compile(r"(?<!\S)(\d{1,3})\.\s+(.{3,250}?\?)")
    matches = list(pattern.finditer(text))
    faqs = []

    for i, match in enumerate(matches):
        question = match.group(2).strip()
        answer_start = match.end()
        answer_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        answer = text[answer_start:answer_end].strip()
        answer = re.sub(r"^[\s:|\-]+", "", answer).strip()
        if question and answer:
            faqs.append({"question": question, "answer": answer})
    return faqs


def score_question(user_question, faq_question, faq_answer=""):
    """Simple lexical matching; no AI model or embedding service."""
    q = normalize(user_question)
    fq = normalize(faq_question)
    ans = normalize(faq_answer)
    if not q or not fq:
        return 0.0

    stop_words = {
        "how", "can", "the", "for", "you", "are", "what", "where",
        "does", "with", "from", "this", "that", "have", "has", "was",
        "who", "why", "when", "which", "and", "but", "not", "its",
        "into", "your", "please", "tell", "about",
    }
    query_words = {w for w in q.split() if len(w) > 2 and w not in stop_words}
    if not query_words:
        return SequenceMatcher(None, q, fq).ratio()

    faq_words = set(fq.split())
    answer_words = set(ans.split())
    question_coverage = sum(w in faq_words for w in query_words) / len(query_words)
    answer_coverage = sum(w in answer_words for w in query_words) / len(query_words)
    similarity = SequenceMatcher(None, q, fq).ratio()
    return 0.55 * question_coverage + 0.30 * similarity + 0.15 * answer_coverage


def search_faq(question, limit=30):
    """Search FAQ chunks, then return the best matching individual FAQ."""
    question = (question or "").strip()
    if not question:
        return None

    app.logger.info("CHAT SEARCH: starting")
    rows = []
    with connect() as conn:
        app.logger.info("CHAT SEARCH: database connected")
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            app.logger.info("CHAT SEARCH: running full-text query")
            cur.execute(
                """
                WITH q AS (
                    SELECT websearch_to_tsquery('english', %s) AS tsq
                )
                SELECT f.id, f.page_number, f.content,
                       ts_rank_cd(f.search_vector, q.tsq) AS text_rank
                FROM faq_chunks AS f
                CROSS JOIN q
                WHERE numnode(q.tsq) > 0
                  AND f.search_vector @@ q.tsq
                ORDER BY ts_rank_cd(f.search_vector, q.tsq) DESC
                LIMIT %s
                """,
                (question, limit),
            )
            rows = cur.fetchall()
            app.logger.info("CHAT SEARCH: full-text rows=%s", len(rows))

            if not rows:
                stop_words = {
                    "how", "can", "the", "for", "you", "are", "what",
                    "where", "does", "with", "from", "this", "that",
                    "have", "has", "was", "who", "why", "when", "which",
                    "and", "but", "not", "its", "into", "your",
                }
                words = list(dict.fromkeys(
                    w.lower() for w in re.findall(r"[a-zA-Z0-9]+", question)
                    if len(w) > 2 and w.lower() not in stop_words
                ))[:8]
                if words:
                    conditions = " OR ".join(["content ILIKE %s"] * len(words))
                    params = [f"%{word}%" for word in words]
                    app.logger.info("CHAT SEARCH: running fallback query")
