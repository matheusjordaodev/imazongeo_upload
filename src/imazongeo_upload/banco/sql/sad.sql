-- SAD: alertas de desmatamento e degradação (mensal).
-- Idempotente. Requer PostgreSQL 15+ e PostGIS 3. Geometrias em EPSG:4326
-- (WGS 84), 2D, MultiPolygon. As colunas seguem o padrão de padrao.py.

-- Cada arquivo recebido traz um tipo de alerta num recorte territorial (camada),
-- com todos os meses. Cada mês de cada tipo/camada é uma carga (particao =
-- 'tipo/camada'): um novo envio substitui só os meses e a parte que ele traz.
-- As camadas são recortes da mesma área: para totais, some uma camada só.

CREATE TABLE IF NOT EXISTS imazongeo.sad_alerta (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    carga_id   bigint NOT NULL REFERENCES imazongeo.carga (id) ON DELETE CASCADE,
    ano        smallint NOT NULL,
    mes        smallint NOT NULL CHECK (mes BETWEEN 1 AND 12),
    tipo       text NOT NULL CHECK (tipo IN ('desmatamento', 'degradacao')),
    camada     text NOT NULL CHECK (camada IN (
                   'amazonia_legal', 'municipios', 'assentamentos', 'terras_indigenas',
                   'unidades_conservacao')),
    sensor     text,
    uf         char(2),
    municipio  text,
    territorio text,                       -- assentamento, TI ou UC
    uso        text,                       -- só unidades_conservacao
    jurisdicao text,                       -- só unidades_conservacao
    area_km2   double precision NOT NULL,
    atributos  jsonb NOT NULL,             -- propriedades do arquivo recebido
    geom       geometry(MultiPolygon, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS sad_alerta_tipo_camada_idx
    ON imazongeo.sad_alerta (tipo, camada, ano, mes);
CREATE INDEX IF NOT EXISTS sad_alerta_carga_idx ON imazongeo.sad_alerta (carga_id);
CREATE INDEX IF NOT EXISTS sad_alerta_geom_idx ON imazongeo.sad_alerta USING gist (geom);

-- Lida pelo dashboard do SAD (colunas pelo nome). Recriada com DROP + CREATE
-- porque pode existir uma versão anterior com outras colunas ou outros tipos
-- (ex.: montada sobre tabelas importadas direto dos GeoJSONs).
DROP VIEW IF EXISTS imazongeo.vw_sad;
CREATE VIEW imazongeo.vw_sad AS
SELECT ano, mes, tipo, camada, sensor, uf, municipio, territorio, uso, jurisdicao,
       area_km2, geom, id
FROM imazongeo.sad_alerta;
