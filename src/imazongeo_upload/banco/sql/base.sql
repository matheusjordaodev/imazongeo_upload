-- Banco do ImazonGeo: esquema e tabelas de controle (comuns a todos os datasets).
-- Idempotente. Requer PostgreSQL 15+ e PostGIS 3. Geometrias em EPSG:4326
-- (WGS 84), 2D, MultiPolygon. As colunas seguem o padrão de padrao.py.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE SCHEMA IF NOT EXISTS imazongeo;

-- ---------------------------------------------------------------------------
-- Controle
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS imazongeo.dataset (
    slug          text PRIMARY KEY,
    nome          text NOT NULL,
    periodicidade text NOT NULL CHECK (periodicidade IN ('mensal', 'trimestral', 'anual')),
    raiz_s3       text NOT NULL
);

-- Cada envio (ou arquivo legado importado do S3) que gravou um período.
-- Um novo envio substitui os dados do período: a carga anterior fica com
-- vigente = false e seus registros são apagados.
-- Nos datasets com várias partes por período (SAD: tipo/camada), cada parte
-- tem a própria carga, identificada por 'particao'.
CREATE TABLE IF NOT EXISTS imazongeo.carga (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dataset        text NOT NULL REFERENCES imazongeo.dataset (slug),
    ano            smallint NOT NULL,
    trimestre      smallint CHECK (trimestre BETWEEN 1 AND 4),
    mes            smallint CHECK (mes BETWEEN 1 AND 12),
    origem         text NOT NULL CHECK (origem IN ('envio', 's3_legado')),
    arquivo        text NOT NULL,          -- nome do arquivo enviado ou chave S3 legada
    sha256         char(64) NOT NULL,
    tamanho_bytes  bigint NOT NULL,
    registros      integer NOT NULL,
    vigente        boolean NOT NULL DEFAULT true,
    particao       text,                   -- ex.: 'desmatamento/municipios' (SAD)
    recebida_em    timestamptz NOT NULL DEFAULT now(),
    substituida_em timestamptz
);
-- Bancos criados antes da coluna particao
ALTER TABLE imazongeo.carga ADD COLUMN IF NOT EXISTS particao text;
DROP INDEX IF EXISTS imazongeo.carga_vigente_uk;
CREATE UNIQUE INDEX carga_vigente_uk ON imazongeo.carga
    (dataset, ano, coalesce(trimestre, 0), coalesce(mes, 0), coalesce(particao, ''))
    WHERE vigente;

-- Arquivos gerados a partir de uma carga e enviados ao S3
CREATE TABLE IF NOT EXISTS imazongeo.publicacao (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    carga_id      bigint NOT NULL REFERENCES imazongeo.carga (id),
    formato       text NOT NULL CHECK (formato IN ('geojson', 'csv', 'shapefile')),
    bucket        text NOT NULL,
    chave_s3      text NOT NULL,
    tamanho_bytes bigint NOT NULL,
    sha256        char(64) NOT NULL,
    publica       boolean NOT NULL,
    publicada_em  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS publicacao_carga_idx ON imazongeo.publicacao (carga_id);
