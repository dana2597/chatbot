from app import ensure_schema, ingest_pdf

if __name__ == "__main__":
    ensure_schema()
    result = ingest_pdf()
    print(f"Indexed {result['chunks']} text chunks from {result['pages']} PDF pages.")
