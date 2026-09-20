-- Honda Civic RAG — esquema do banco (PostgreSQL 16 + pgvector)
-- Executado automaticamente na primeira subida do container
-- (montado em /docker-entrypoint-initdb.d). Para recriar: docker compose down -v

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Fonte (manual de serviço; catálogo de peças no futuro)
CREATE TABLE documents (
  id              SERIAL PRIMARY KEY,
  title           TEXT NOT NULL,
  doc_type        TEXT NOT NULL CHECK (doc_type IN ('service_manual', 'parts_catalog')),
  model_years     INT4RANGE,              -- '[1992,1996)'
  engines         TEXT[],                 -- {D15B7,D15B8,D15Z1,D16Z6}
  file_path       TEXT NOT NULL,
  file_sha256     TEXT NOT NULL UNIQUE,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE sections (
  id              SERIAL PRIMARY KEY,
  document_id     INT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  number          TEXT,                   -- '6'
  title           TEXT NOT NULL,          -- 'Cylinder Head/Valve Train'
  pdf_page_start  INT,
  pdf_page_end    INT
);

-- Sumário com links do PDF (data/meta/toc_links.json)
CREATE TABLE toc_entries (
  id              SERIAL PRIMARY KEY,
  document_id     INT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  ord             INT NOT NULL,
  title           TEXT NOT NULL,
  pdf_page        INT NOT NULL,
  section_id      INT REFERENCES sections(id) ON DELETE SET NULL,
  UNIQUE (document_id, ord)
);

CREATE TABLE pages (
  id              SERIAL PRIMARY KEY,
  document_id     INT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  pdf_page        INT NOT NULL,
  page_label      TEXT,                   -- '6-3' (usado nas citações)
  section_id      INT REFERENCES sections(id) ON DELETE SET NULL,
  image_path      TEXT NOT NULL,
  view_path       TEXT,                   -- versão leve para a UI
  ocr_text        TEXT,
  ocr_engine      TEXT,
  ocr_confidence  REAL,
  layout_json     JSONB,
  needs_review    BOOLEAN NOT NULL DEFAULT FALSE,
  UNIQUE (document_id, pdf_page)
);
CREATE INDEX pages_label_idx ON pages (document_id, page_label);

-- Procedimento = chunk pai
CREATE TABLE procedures (
  id              SERIAL PRIMARY KEY,
  section_id      INT REFERENCES sections(id) ON DELETE CASCADE,
  toc_entry_id    INT REFERENCES toc_entries(id) ON DELETE SET NULL,
  component       TEXT NOT NULL,
  procedure_type  TEXT NOT NULL CHECK (procedure_type IN (
                    'removal','installation','inspection','adjustment','replacement',
                    'overhaul','illustrated_index','troubleshooting','specs','description','other')),
  title           TEXT NOT NULL,
  steps           JSONB,                  -- [{n, text, notes[], figure_ids[]}]
  warnings        TEXT[],
  consumables     TEXT[],
  applicability   JSONB,                  -- {"engine":["D16Z6"],"trans":["MT","AT"]}
  page_labels     TEXT[],
  full_text       TEXT NOT NULL
);
CREATE INDEX procedures_applicability_idx ON procedures USING gin (applicability);

-- Chunk filho = unidade de busca
CREATE TABLE chunks (
  id              SERIAL PRIMARY KEY,
  procedure_id    INT REFERENCES procedures(id) ON DELETE CASCADE,
  chunk_type      TEXT NOT NULL CHECK (chunk_type IN ('procedure','spec_table','figure','flowchart','text')),
  text            TEXT NOT NULL,          -- com prefixo de contexto
  page_labels     TEXT[] NOT NULL,
  applicability   JSONB,
  embedding       VECTOR(1024),           -- bge-m3
  embedding_model TEXT,
  tsv             TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', text)) STORED
);
CREATE INDEX chunks_embedding_idx     ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX chunks_tsv_idx           ON chunks USING gin (tsv);
CREATE INDEX chunks_applicability_idx ON chunks USING gin (applicability);

CREATE TABLE figures (
  id              SERIAL PRIMARY KEY,
  page_id         INT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
  bbox            INT[],                  -- [x0, y0, x1, y1] em px do render 300 DPI
  image_path      TEXT NOT NULL,
  figure_type     TEXT CHECK (figure_type IN (
                    'exploded_view','procedure_illustration','flowchart',
                    'wiring_diagram','table_image','other')),
  caption         TEXT,                   -- gerada pela VLM
  callouts        JSONB,                  -- [{label, fastener, torque, note, bbox}]
  structured      JSONB                   -- fluxograma convertido etc.
);

CREATE TABLE chunk_figures (
  chunk_id        INT REFERENCES chunks(id) ON DELETE CASCADE,
  figure_id       INT REFERENCES figures(id) ON DELETE CASCADE,
  link_type       TEXT CHECK (link_type IN ('same_page','same_column','referenced','callout_match')),
  PRIMARY KEY (chunk_id, figure_id)
);

-- Especificações: a tabela mais valiosa do sistema
CREATE TABLE specs (
  id              SERIAL PRIMARY KEY,
  document_id     INT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  component       TEXT NOT NULL,          -- 'Cylinder head bolts'
  parameter       TEXT NOT NULL,          -- torque|clearance|standard|service_limit|height...
  value_text      TEXT NOT NULL,          -- literal: '73 N·m (7.3 kg-m, 53 lb-ft)'
  value_min       NUMERIC,
  value_max       NUMERIC,
  unit            TEXT,
  fastener        TEXT,                   -- '10 x 1.25 mm'
  side            TEXT CHECK (side IN ('IN','EX')),
  conditions      TEXT,
  applicability   JSONB,
  page_id         INT REFERENCES pages(id) ON DELETE SET NULL,
  figure_id       INT REFERENCES figures(id) ON DELETE SET NULL,
  procedure_id    INT REFERENCES procedures(id) ON DELETE SET NULL,
  source_engine   TEXT CHECK (source_engine IN ('ocr','vlm','both','manual')),
  verified        BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX specs_component_trgm_idx ON specs USING gin (component gin_trgm_ops);
CREATE INDEX specs_applicability_idx  ON specs USING gin (applicability);

CREATE TABLE special_tools (
  document_id     INT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  tool_number     TEXT NOT NULL,          -- '07JAA-001020A'
  name            TEXT,
  section_id      INT REFERENCES sections(id) ON DELETE SET NULL,
  page_id         INT REFERENCES pages(id) ON DELETE SET NULL,
  figure_id       INT REFERENCES figures(id) ON DELETE SET NULL,
  PRIMARY KEY (document_id, tool_number)
);

CREATE TABLE part_numbers (
  id              SERIAL PRIMARY KEY,
  document_id     INT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  part_number     TEXT NOT NULL,          -- '08718-0001'
  description     TEXT,                   -- 'liquid gasket'
  ref_number      TEXT,                   -- nº no diagrama do catálogo (futuro)
  applicability   JSONB,
  page_id         INT REFERENCES pages(id) ON DELETE SET NULL,
  figure_id       INT REFERENCES figures(id) ON DELETE SET NULL,
  verified        BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX part_numbers_number_idx ON part_numbers (part_number);
CREATE INDEX part_numbers_desc_trgm_idx ON part_numbers USING gin (description gin_trgm_ops);

CREATE TABLE dtc_codes (
  document_id     INT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  code            TEXT NOT NULL,          -- '21'
  system          TEXT,                   -- 'VTEC spool valve'
  description     TEXT,
  procedure_id    INT REFERENCES procedures(id) ON DELETE SET NULL,
  PRIMARY KEY (document_id, code)
);

CREATE TABLE glossary_pt_en (
  term_pt         TEXT PRIMARY KEY,
  term_en         TEXT[] NOT NULL
);

-- Divergências OCR × VLM, baixa confiança, mm × in incoerente
CREATE TABLE review_queue (
  id              SERIAL PRIMARY KEY,
  item_type       TEXT NOT NULL CHECK (item_type IN ('spec','page','part_number','figure','table')),
  item_id         INT,
  reason          TEXT NOT NULL,          -- ocr_vlm_mismatch|low_conf|unit_mismatch
  candidates      JSONB,
  resolved        BOOLEAN NOT NULL DEFAULT FALSE,
  resolution      TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX review_queue_open_idx ON review_queue (resolved, item_type);

CREATE TABLE eval_questions (
  id              SERIAL PRIMARY KEY,
  question_pt     TEXT NOT NULL,
  intent          TEXT,
  engine          TEXT,
  expected_pages  TEXT[],
  expected_values TEXT[]
);

-- Glossário inicial PT → EN
INSERT INTO glossary_pt_en (term_pt, term_en) VALUES
  ('correia dentada',        '{timing belt}'),
  ('tampa de válvulas',      '{cylinder head cover,valve cover}'),
  ('tampa do cabeçote',      '{cylinder head cover}'),
  ('junta do cabeçote',      '{cylinder head gasket}'),
  ('cabeçote',               '{cylinder head}'),
  ('parafusos do cabeçote',  '{cylinder head bolts}'),
  ('folga de válvulas',      '{valve clearance}'),
  ('comando de válvulas',    '{camshaft}'),
  ('balancim',               '{rocker arm}'),
  ('retentor de válvula',    '{valve seal,valve stem seal}'),
  ('bomba d''água',          '{water pump}'),
  ('bomba de óleo',          '{oil pump}'),
  ('filtro de óleo',         '{oil filter}'),
  ('bomba da direção',       '{power steering pump,P/S pump}'),
  ('direção hidráulica',     '{power steering}'),
  ('compressor do ar',       '{A/C compressor}'),
  ('coxim do motor',         '{engine mount}'),
  ('distribuidor',           '{distributor}'),
  ('vela de ignição',        '{spark plug}'),
  ('pinça de freio',         '{brake caliper}'),
  ('pastilha de freio',      '{brake pads}'),
  ('disco de freio',         '{brake disc,rotor}'),
  ('embreagem',              '{clutch}'),
  ('câmbio',                 '{transmission,transaxle}'),
  ('homocinética',           '{CV joint,driveshaft}'),
  ('bieleta',                '{stabilizer link}'),
  ('pivô',                   '{ball joint}'),
  ('manga de eixo',          '{knuckle}'),
  ('cubo de roda',           '{hub}'),
  ('radiador',               '{radiator}'),
  ('válvula termostática',   '{thermostat}'),
  ('coletor de admissão',    '{intake manifold}'),
  ('coletor de escape',      '{exhaust manifold}'),
  ('luz da injeção',         '{Check Engine light,malfunction indicator lamp}'),
  ('código de falha',        '{self-diagnosis code,trouble code}'),
  ('torque',                 '{torque,N·m}'),
  ('limite de serviço',      '{service limit}'),
  ('empeno',                 '{warpage}'),
  ('folga',                  '{clearance,play}'),
  ('remoção',                '{removal}'),
  ('instalação',             '{installation}'),
  ('inspeção',               '{inspection}'),
  ('regulagem',              '{adjustment}'),
  ('ar condicionado',        '{A/C,air conditioner}'),
  ('compressor do ar condicionado', '{A/C compressor}'),
  ('bomba de combustível',   '{fuel pump}'),
  ('ferramenta especial',    '{special tool}'),
  ('guia de válvula',        '{valve guide}'),
  ('selante líquido',        '{liquid gasket}'),
  ('junta',                  '{gasket}'),
  ('remover',                '{remove,removal}'),
  ('correia',                '{belt}');
