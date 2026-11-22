CREATE EXTENSION IF NOT EXISTS vector;

-- Services the clinic offers (GP consult, vaccination, health screening, ...).
CREATE TABLE services (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name             text          NOT NULL UNIQUE,
    description      text,
    duration_minutes integer       NOT NULL CHECK (duration_minutes > 0),
    price_myr        numeric(8, 2) CHECK (price_myr >= 0),
    is_active        boolean       NOT NULL DEFAULT true,
    created_at       timestamptz   NOT NULL DEFAULT now()
);

-- Clinic-approved FAQ text, split into chunks for retrieval.
-- Keep the vector size equal to your embedding model's output size.
CREATE TABLE faq_chunks (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source      text        NOT NULL,              -- e.g. 'faq.md#opening-hours'
    language    text        NOT NULL DEFAULT 'en'
                CHECK (language IN ('en', 'ms', 'zh')),
    content     text        NOT NULL,
    embedding   vector(1536),                      -- NULL until embedded
    content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Vector (semantic) and keyword indexes, for hybrid search.
CREATE INDEX faq_chunks_embedding_idx ON faq_chunks
    USING hnsw (embedding vector_cosine_ops);
CREATE INDEX faq_chunks_content_tsv_idx ON faq_chunks USING gin (content_tsv);

CREATE TABLE doctors (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    full_name  text        NOT NULL,
    specialty  text        NOT NULL DEFAULT 'General Practice',
    languages  text[]      NOT NULL DEFAULT '{en}',
    is_active  boolean     NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Bookable time slots, one row per doctor per time window.
CREATE TABLE slots (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    doctor_id bigint      NOT NULL REFERENCES doctors (id) ON DELETE CASCADE,
    starts_at timestamptz NOT NULL,
    ends_at   timestamptz NOT NULL,
    CHECK (ends_at > starts_at),
    UNIQUE (doctor_id, starts_at),
    UNIQUE (id, doctor_id)   -- lets bookings reference (slot, doctor) as a pair
);

CREATE INDEX slots_starts_at_idx ON slots (starts_at);

-- One chat thread on one channel.
CREATE TABLE conversations (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    channel         text        NOT NULL CHECK (channel IN ('whatsapp', 'web')),
    external_id     text        NOT NULL,   -- WhatsApp phone (E.164) or web session id
    language        text        CHECK (language IN ('en', 'ms', 'zh')),
    status          text        NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'handed_off', 'closed')),
    started_at      timestamptz NOT NULL DEFAULT now(),
    last_message_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE messages (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conversation_id bigint      NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    role            text        NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
    content         text        NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX conversations_channel_external_idx ON conversations (channel, external_id);

CREATE TABLE bookings (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    doctor_id       bigint      NOT NULL,
    slot_id         bigint      NOT NULL,
    service_id      bigint      NOT NULL REFERENCES services (id),
    conversation_id bigint      REFERENCES conversations (id) ON DELETE SET NULL,
    patient_name    text        NOT NULL,
    patient_phone   text        NOT NULL,   -- E.164, e.g. +60123456789
    status          text        NOT NULL DEFAULT 'confirmed'
                    CHECK (status IN ('confirmed', 'cancelled', 'completed', 'no_show')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    -- The slot must belong to the same doctor.
    FOREIGN KEY (slot_id, doctor_id) REFERENCES slots (id, doctor_id)
);

-- One live booking per doctor + slot. Cancelled bookings don't count, so a
-- cancelled slot can be booked again.
CREATE UNIQUE INDEX bookings_doctor_slot_uniq ON bookings (doctor_id, slot_id)
    WHERE status <> 'cancelled';
