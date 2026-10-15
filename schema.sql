CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS faq_chunks (
    id BIGSERIAL PRIMARY KEY,
    source_file TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    chunk_number INTEGER NOT NULL DEFAULT 1,
    content TEXT NOT NULL,
    search_vector TSVECTOR GENERATED ALWAYS AS
        (to_tsvector('english', coalesce(content, ''))) STORED,
    embedding VECTOR(256),
    UNIQUE (source_file, page_number, chunk_number)
);

CREATE INDEX IF NOT EXISTS faq_chunks_search_idx
    ON faq_chunks USING GIN (search_vector);

CREATE INDEX IF NOT EXISTS faq_chunks_embedding_idx
    ON faq_chunks USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 10);
