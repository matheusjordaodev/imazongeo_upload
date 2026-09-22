-- Ameaça & Pressão: ranking trimestral de áreas protegidas.
-- Idempotente. Requer PostgreSQL 15+ e PostGIS 3. Geometrias em EPSG:4326
-- (WGS 84), 2D, MultiPolygon. As colunas seguem o padrão de padrao.py.

-- A mesma área (e geometria) aparece em vários recortes e trimestres; a
-- geometria fica uma única vez em ap_area_protegida.

CREATE TABLE IF NOT EXISTS imazongeo.ap_area_protegida (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    modalidade text NOT NULL CHECK (modalidade IN ('ti', 'ucf', 'uce')),
    nome       text NOT NULL,
    categoria  text,
    uso        text,
    jurisdicao text,
    estado     text,
    geom_md5   char(32) NOT NULL,          -- md5 do WKB: versões da geometria
    geom       geometry(MultiPolygon, 4326) NOT NULL,
    CONSTRAINT ap_area_protegida_uk UNIQUE (modalidade, nome, geom_md5)
);
CREATE INDEX IF NOT EXISTS ap_area_protegida_geom_idx
    ON imazongeo.ap_area_protegida USING gist (geom);

CREATE TABLE IF NOT EXISTS imazongeo.ap_ranking (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    carga_id  bigint NOT NULL REFERENCES imazongeo.carga (id) ON DELETE CASCADE,
    area_id   bigint NOT NULL REFERENCES imazongeo.ap_area_protegida (id),
    ano       smallint NOT NULL,
    trimestre smallint NOT NULL CHECK (trimestre BETWEEN 1 AND 4),
    legenda   text,
    recorte   text NOT NULL CHECK (recorte IN ('geral', 'categ_ti', 'categ_uce', 'categ_ucf')),
    classe    text NOT NULL CHECK (classe IN ('ameaca', 'pressao')),
    posicao   smallint NOT NULL,
    celulas   integer NOT NULL,
    atributos jsonb NOT NULL,
    CONSTRAINT ap_ranking_uk UNIQUE (ano, trimestre, recorte, classe, area_id)
);
CREATE INDEX IF NOT EXISTS ap_ranking_area_idx ON imazongeo.ap_ranking (area_id);
CREATE INDEX IF NOT EXISTS ap_ranking_carga_idx ON imazongeo.ap_ranking (carga_id);

CREATE OR REPLACE VIEW imazongeo.vw_ameaca_pressao AS
SELECT r.ano, r.trimestre, r.legenda, r.recorte, r.classe, r.posicao, r.celulas,
       a.nome, a.modalidade, a.categoria, a.uso, a.jurisdicao, a.estado,
       a.geom, r.id, r.area_id
FROM imazongeo.ap_ranking r
JOIN imazongeo.ap_area_protegida a ON a.id = r.area_id;
