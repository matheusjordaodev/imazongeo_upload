-- SIMEX: polígonos de exploração madeireira (anual).
-- Idempotente. Requer PostgreSQL 15+ e PostGIS 3. Geometrias em EPSG:4326
-- (WGS 84), 2D, MultiPolygon. As colunas seguem o padrão de padrao.py.

-- A camada 'municipios' cobre toda a exploração do ano; as demais são recortes
-- dela. Para totais, some uma camada só (ex.: camada = 'municipios').

CREATE TABLE IF NOT EXISTS imazongeo.simex_exploracao (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    carga_id   bigint NOT NULL REFERENCES imazongeo.carga (id) ON DELETE CASCADE,
    ano        smallint NOT NULL,
    camada     text NOT NULL CHECK (camada IN (
                   'municipios', 'imoveis_rurais', 'assentamentos', 'terras_indigenas',
                   'unidades_conservacao', 'terras_nao_destinadas')),
    categoria  text CHECK (categoria IN ('autorizada', 'não autorizada', 'análise')),
    uf         char(2),
    municipio  text,
    cod_mun    integer,
    territorio text,
    subclasse  text,
    area_ha    double precision NOT NULL,
    atributos  jsonb NOT NULL,             -- propriedades do arquivo recebido
    geom       geometry(MultiPolygon, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS simex_exploracao_ano_camada_idx
    ON imazongeo.simex_exploracao (ano, camada);
CREATE INDEX IF NOT EXISTS simex_exploracao_mun_idx ON imazongeo.simex_exploracao (cod_mun);
CREATE INDEX IF NOT EXISTS simex_exploracao_carga_idx ON imazongeo.simex_exploracao (carga_id);
CREATE INDEX IF NOT EXISTS simex_exploracao_geom_idx
    ON imazongeo.simex_exploracao USING gist (geom);

CREATE OR REPLACE VIEW imazongeo.vw_simex AS
SELECT ano, camada, categoria, uf, municipio, cod_mun, territorio, subclasse, area_ha,
       geom, id
FROM imazongeo.simex_exploracao;

CREATE OR REPLACE VIEW imazongeo.vw_simex_municipio_ano AS
SELECT ano, uf, cod_mun, municipio,
       sum(area_ha) FILTER (WHERE categoria = 'autorizada')     AS area_autorizada_ha,
       sum(area_ha) FILTER (WHERE categoria = 'não autorizada') AS area_nao_autorizada_ha,
       sum(area_ha) FILTER (WHERE categoria = 'análise')        AS area_em_analise_ha,
       sum(area_ha) FILTER (WHERE categoria IS NULL)            AS area_sem_categoria_ha,
       sum(area_ha)                                             AS area_total_ha
FROM imazongeo.simex_exploracao
WHERE camada = 'municipios'
GROUP BY ano, uf, cod_mun, municipio;
