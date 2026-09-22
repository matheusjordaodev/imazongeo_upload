-- Floreser: vegetação secundária por município, UF e idade (anual).
-- Idempotente. Requer PostgreSQL 15+ e PostGIS 3. Geometrias em EPSG:4326
-- (WGS 84), 2D, MultiPolygon. As colunas seguem o padrão de padrao.py.

-- Municípios que cruzam divisa estadual aparecem com mais de um cod_uf.

CREATE TABLE IF NOT EXISTS imazongeo.floreser_municipio (
    carga_id  bigint NOT NULL REFERENCES imazongeo.carga (id) ON DELETE CASCADE,
    ano       smallint NOT NULL,
    cod_uf    smallint NOT NULL,
    estado    text NOT NULL,
    cod_mun   integer NOT NULL,
    municipio text NOT NULL,
    idade     smallint NOT NULL,
    area_ha   double precision NOT NULL,
    PRIMARY KEY (ano, cod_uf, cod_mun, idade)
);
CREATE INDEX IF NOT EXISTS floreser_municipio_mun_idx
    ON imazongeo.floreser_municipio (cod_mun, ano);
CREATE INDEX IF NOT EXISTS floreser_municipio_carga_idx
    ON imazongeo.floreser_municipio (carga_id);

CREATE OR REPLACE VIEW imazongeo.vw_floreser AS
SELECT ano, cod_uf, estado, cod_mun, municipio, idade, area_ha
FROM imazongeo.floreser_municipio;
