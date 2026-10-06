-- Visão do SAD, em arquivo próprio: na migração das tabelas cruas ela é
-- trocada só no fim, para não travar o dashboard durante a carga.

-- Lida pelo dashboard do SAD (colunas pelo nome). Recriada com DROP + CREATE
-- porque pode existir uma versão anterior com outras colunas ou outros tipos
-- (ex.: montada sobre tabelas importadas direto dos GeoJSONs).
DROP VIEW IF EXISTS imazongeo.vw_sad;
CREATE VIEW imazongeo.vw_sad AS
SELECT ano, mes, tipo, camada, sensor, uf, municipio, territorio, uso, jurisdicao,
       area_km2, geom, id
FROM imazongeo.sad_alerta;
