
import os
import re
import html
from difflib import SequenceMatcher

from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor

load_dotenv()

app = Flask(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")


def connect():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is missing. Configure it in your .env file."
        )

    return psycopg2.connect(DATABASE_URL)


def clean_text(text):
    """Normalize extracted PDF text for searching and display."""
    text = html.unescape(text or "")
    text = text.replace("\x00", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize(text):
    text = clean_text(text).lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_faqs(content):
    """
    Extract numbered FAQ question-and-answer pairs from a PDF chunk.

    Expected extracted text:
    1. Question? Answer.
    2. Question? Answer.
    """
    text = clean_text(content)

    # Remove repeated PDF headers and footers when present.
    text = re.sub(
        r"Page\s+\d+\s+of\s+\d+",
        " ",
        text,
        flags=re.IGNORECASE
    )
    text = re.sub(
        r"Frequently Asked Questions \(FAQs\) on GST Law and procedures",
        " ",
        text,
        flags=re.IGNORECASE
    )

    # Find numbered FAQ questions.
    pattern = re.compile(
        r"(?<!\S)(\d{1,3})\.\s+"
        r"(.*?)"
        r"\?"
        r"\s*(.*?)"
        r"(?=(?:\s+\d{1,3}\.\s+)|$)",
        re.DOTALL
    )

    faqs = []

    for match in pattern.finditer(text):
        question = clean_text(match.group(2)) + "?"
        answer = clean_text(match.group(3))

        # Ignore incomplete or empty entries.
        if len(question) > 5 and answer:
            faqs.append({
                "question": question,
                "answer": answer
            })

    return faqs


def score_question(user_question, faq_question, faq_answer):
    """Simple deterministic text matching; no AI model is used."""
    q = normalize(user_question)
    faq_q = normalize(faq_question)
    answer = normalize(faq_answer)

    if not q or not faq_q:
        return 0

    similarity = SequenceMatcher(None, q, faq_q).ratio()

    stop_words = {
        "how", "can", "the", "for", "you", "are", "what",
        "where", "does", "with", "from", "this", "that",
        "have", "has", "was", "is", "a", "an", "to", "of",
        "in", "on", "i", "my", "it", "be"
    }

    words = {
        word for word in q.split()
        if len(word) > 2 and word not in stop_words
    }

    if not words:
        return similarity

    question_words = set(faq_q.split())
    answer_words = set(answer.split())

    question_matches = sum(word in question_words for word in words)
    answer_matches = sum(word in answer_words for word in words)

    coverage = question_matches / len(words)
    answer_coverage = answer_matches / len(words)

    return (
        similarity * 0.45
        + coverage * 0.45
        + answer_coverage * 0.10
    )


def search_faq(question, limit=100):
    """
    Search existing indexed chunks, extract individual FAQs,
    and return only the best matching FAQ answer.
    """
    question = clean_text(question)

    if not question:
        return None

    with connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # First: PostgreSQL full-text search.
            cur.execute(
                """
                WITH q AS (
                    SELECT websearch_to_tsquery('english', %s) AS tsq
                )
                SELECT page_number, content
                FROM faq_chunks, q
                WHERE numnode(q.tsq) > 0
                  AND search_vector @@ q.tsq
                ORDER BY ts_rank_cd(search_vector, q.tsq) DESC
                LIMIT %s
                """,
                (question, limit)
            )
            rows = cur.fetchall()

            # Fallback: search chunks using meaningful query words.
            if not rows:
                stop_words = {
                    "how", "can", "the", "for", "you", "are",
                    "what", "where", "does", "with", "from",
                    "this", "that", "have", "has", "was"
                }

                words = list(dict.fromkeys(
                    word.lower()
                    for word in re.findall(r"[a-zA-Z0-9]+", question)
                    if len(word) > 2 and word.lower() not in stop_words
                ))

                if words:
                    conditions = " OR ".join(
                        ["content ILIKE %s"] * len(words)
                    )
                    params = [f"%{word}%" for word in words]

                    cur.execute(
                        f"""
                        SELECT page_number, content
                        FROM faq_chunks
                        WHERE {conditions}
                        ORDER BY page_number
                        LIMIT %s
                        """,
                        params + [limit]
                    )
                    rows = cur.fetchall()

    # Extract individual questions and answers from matching chunks.
    candidates = []

    for row in rows:
        faqs = extract_faqs(row["content"])

        for faq in faqs:
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

    candidates.sort(key=lambda item: item["score"], reverse=True)
    best = candidates[0]

    # Avoid returning a random FAQ for an unrelated question.
    if best["score"] < 0.30:
        return None

    return best


@app.route("/")
def home():
    return render_template("index.html")


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


@app.route("/api/ask", methods=["POST"])
def ask():
    data = request.get_json(silent=True) or {}
    question = clean_text(data.get("question", ""))

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
                    "I couldn't find a matching answer in the uploaded "
                    "GST FAQ PDF. Try different keywords or a more "
                    "specific question."
                ),
                "sources": []
            })

        return jsonify({
            "answer": result["answer"],
            "sources": [{
                "page": result["page"],
                "question": result["question"]
            }]
        })

    except Exception:
        app.logger.exception("Question search failed")
        return jsonify({
            "answer": (
                "There was a database search error. Check your "
                "DATABASE_URL and PostgreSQL connection."
            ),
            "sources": []
        }), 500


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=os.getenv("FLASK_DEBUG", "0") == "1"
    )
