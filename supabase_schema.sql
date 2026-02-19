-- Users: project managers and admins
CREATE TABLE IF NOT EXISTS users (
    id            UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    name          TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'pm',  -- 'pm' | 'admin'
    approved      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Projects: one per contract/job
CREATE TABLE IF NOT EXISTS projects (
    id               UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    name             TEXT NOT NULL,
    contract_pdf_path TEXT,
    user_id          UUID REFERENCES users(id),
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);

-- Reusable product library — cached across all projects
CREATE TABLE IF NOT EXISTS products (
    id                  UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    brand               TEXT,
    model_number        TEXT NOT NULL UNIQUE,
    product_name        TEXT,
    manual_source_url   TEXT,
    manual_storage_path TEXT,
    last_verified       TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- Line items extracted from a specific contract
CREATE TABLE IF NOT EXISTS project_items (
    id            UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    project_id    UUID REFERENCES projects(id) ON DELETE CASCADE,
    product_id    UUID REFERENCES products(id),
    raw_line_item TEXT,
    brand         TEXT,
    model_number  TEXT,
    product_name  TEXT,
    status        TEXT DEFAULT 'pending',  -- pending | found | not_found | manual_entry
    manual_url    TEXT,
    notes         TEXT,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);
