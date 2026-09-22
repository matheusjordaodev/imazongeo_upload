"""Acesso ao PostgreSQL/PostGIS: esquema, gravação dos envios e leitura no padrão.

A conexão vem de ``DATABASE_URL`` (ex.: postgresql://usuario:senha@host/imazongeo).
Cada processamento abre a própria conexão: a interface web roda os jobs em
threads e o psycopg2 não permite compartilhar uma conexão entre elas.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import psycopg2
import shapely
from psycopg2.extras import execute_values

from .correcao import PERDIDO, nome_conhecido
from .entrada import Envio
from .padrao import SRID, TODOS, Padrao, Periodo

SQL_DIR = Path(__file__).with_name("sql")

# Tabela com os registros de cada dataset (apagados ao substituir o período)
TABELAS = {
    "simex": "simex_exploracao",
    "ameaca_pressao": "ap_ranking",
    "floreser": "floreser_municipio",
}


def database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL não configurada. Defina no .env, por exemplo:\n"
            "  DATABASE_URL=postgresql://usuario:senha@localhost:5432/imazongeo"
        )
    return url


@contextmanager
def conectar(url: str | None = None) -> Iterator[Any]:
    """Conexão nova (fechada ao sair). Transações ficam a cargo de quem usa."""
    conn = psycopg2.connect(url or database_url())
    try:
        yield conn
    finally:
        conn.close()


def criar_schema(conn, datasets: list[str] | None = None) -> None:
    """Cria/atualiza as tabelas de controle e as dos datasets (None = todos)."""
    datasets = list(TODOS) if datasets is None else datasets
    with conn, conn.cursor() as cur:
        cur.execute((SQL_DIR / "base.sql").read_text(encoding="utf-8"))
        for slug in datasets:
            cur.execute(
                (SQL_DIR / f"{TODOS[slug].slug}.sql").read_text(encoding="utf-8")
            )
        execute_values(
            cur,
            """INSERT INTO imazongeo.dataset (slug, nome, periodicidade, raiz_s3)
               VALUES %s
               ON CONFLICT (slug) DO UPDATE SET nome = EXCLUDED.nome,
                   periodicidade = EXCLUDED.periodicidade,
                   raiz_s3 = EXCLUDED.raiz_s3""",
            [
                (p.slug, p.nome, p.periodicidade, p.raiz_s3)
                for p in (TODOS[slug] for slug in datasets)
            ],
        )
    logging.info("Esquema imazongeo criado/atualizado: %s.", ", ".join(datasets))


def _filtro_periodo(periodo: Periodo) -> tuple[str, tuple]:
    return (
        "ano = %s AND trimestre IS NOT DISTINCT FROM %s"
        " AND mes IS NOT DISTINCT FROM %s AND particao IS NULL",
        (periodo.ano, periodo.trimestre, periodo.mes),
    )


def carga_vigente(cur, dataset: str, periodo: Periodo) -> tuple | None:
    """(id, sha256) da carga vigente do período, ou None."""
    filtro, valores = _filtro_periodo(periodo)
    cur.execute(
        f"""SELECT id, sha256 FROM imazongeo.carga
            WHERE dataset = %s AND vigente AND {filtro}""",
        (dataset, *valores),
    )
    return cur.fetchone()


# ---------------------------------------------------------------------------
# Gravação
# ---------------------------------------------------------------------------


def gravar_envio(cur, envio: Envio, origem: str = "envio") -> int:
    """Grava o envio como carga vigente do período, substituindo a anterior.

    Roda dentro da transação de ``cur``; quem chama decide commit/rollback.
    """
    p, periodo = envio.padrao, envio.periodo
    # Serializa envios simultâneos do mesmo período
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))", (f"imazongeo:{p.slug}:{periodo}",)
    )
    filtro, valores = _filtro_periodo(periodo)
    cur.execute(
        f"""UPDATE imazongeo.carga SET vigente = false, substituida_em = now()
            WHERE dataset = %s AND vigente AND {filtro} RETURNING id""",
        (p.slug, *valores),
    )
    anteriores = [r[0] for r in cur.fetchall()]
    if anteriores:
        cur.execute(
            f"DELETE FROM imazongeo.{TABELAS[p.slug]} WHERE carga_id = ANY(%s)",
            (anteriores,),
        )
        logging.info(
            "%s %s: %d registro(s) da carga anterior substituídos",
            p.slug,
            periodo,
            cur.rowcount,
        )
    cur.execute(
        """INSERT INTO imazongeo.carga (dataset, ano, trimestre, mes, origem, arquivo,
               sha256, tamanho_bytes, registros)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (
            p.slug,
            periodo.ano,
            periodo.trimestre,
            periodo.mes,
            origem,
            envio.arquivo,
            envio.sha256,
            envio.tamanho_bytes,
            envio.registros,
        ),
    )
    carga_id = cur.fetchone()[0]
    if p.slug == "ameaca_pressao":
        _corrigir_nomes_envio(cur, envio)
    _INSERIR[p.slug](cur, carga_id, envio)
    if p.slug == "ameaca_pressao":
        _corrigir_nomes_gravados(cur)
        cur.execute(
            """DELETE FROM imazongeo.ap_area_protegida a WHERE NOT EXISTS
               (SELECT 1 FROM imazongeo.ap_ranking r WHERE r.area_id = a.id)"""
        )
    return carga_id


def _json(p: dict) -> str:
    return json.dumps(p, ensure_ascii=False, default=str)


def _inserir_simex(cur, carga_id: int, envio: Envio) -> None:
    campos = ["ano", "camada", "categoria", "uf", "municipio", "cod_mun", "territorio",
              "subclasse", "area_ha"]  # fmt: skip
    execute_values(
        cur,
        f"""INSERT INTO imazongeo.simex_exploracao
                (carga_id, {", ".join(campos)}, atributos, geom) VALUES %s""",
        [
            (carga_id, *(linha[c] for c in campos), _json(p), psycopg2.Binary(w))
            for linha, p, w in zip(
                envio.linhas, envio.atributos, envio.wkbs, strict=True
            )
        ],
        template=f"({', '.join(['%s'] * (len(campos) + 2))}::jsonb,"
        f" ST_GeomFromWKB(%s, {SRID}))",
        page_size=500,
    )


def _nomes_conhecidos(cur, modalidade: str) -> list[str]:
    cur.execute(
        """SELECT DISTINCT nome FROM imazongeo.ap_area_protegida
           WHERE modalidade = %s AND strpos(nome, %s) = 0""",
        (modalidade, PERDIDO),
    )
    return [r[0] for r in cur.fetchall()]


def _corrigir_nomes_envio(cur, envio: Envio) -> None:
    """Nomes com letra perdida (``�``) → nome já conhecido no banco ou no envio."""
    for linha in envio.linhas:
        if PERDIDO not in linha["nome"]:
            continue
        conhecidos = _nomes_conhecidos(cur, linha["modalidade"]) + [
            outra["nome"]
            for outra in envio.linhas
            if outra["modalidade"] == linha["modalidade"]
        ]
        novo = nome_conhecido(linha["nome"], conhecidos)
        if novo:
            logging.warning("Nome corrigido: %s → %s", linha["nome"], novo)
            linha["nome"] = novo


def _corrigir_nomes_gravados(cur) -> None:
    """Corrige áreas gravadas com ``�`` quando o nome correto passa a existir.

    Se a área corrigida já existe (mesmo nome e geometria), os rankings passam
    a apontar para ela e a versão com ``�`` é apagada.
    """
    cur.execute(
        """SELECT id, modalidade, nome, geom_md5 FROM imazongeo.ap_area_protegida
           WHERE strpos(nome, %s) > 0""",
        (PERDIDO,),
    )
    for area_id, modalidade, nome, md5 in cur.fetchall():
        novo = nome_conhecido(nome, _nomes_conhecidos(cur, modalidade))
        if novo is None:
            continue
        cur.execute(
            """SELECT id FROM imazongeo.ap_area_protegida
               WHERE modalidade = %s AND nome = %s AND geom_md5 = %s""",
            (modalidade, novo, md5),
        )
        existente = cur.fetchone()
        if existente:
            cur.execute(
                "UPDATE imazongeo.ap_ranking SET area_id = %s WHERE area_id = %s",
                (existente[0], area_id),
            )
            cur.execute(
                "DELETE FROM imazongeo.ap_area_protegida WHERE id = %s", (area_id,)
            )
        else:
            cur.execute(
                "UPDATE imazongeo.ap_area_protegida SET nome = %s WHERE id = %s",
                (novo, area_id),
            )
        logging.warning("Nome corrigido no banco: %s → %s", nome, novo)


def _inserir_ap(cur, carga_id: int, envio: Envio) -> None:
    # A mesma área aparece no recorte geral e no da categoria: grava uma vez.
    # Os atributos descritivos da área ficam os do envio mais recente.
    areas = {}
    for linha, w in zip(envio.linhas, envio.wkbs, strict=True):
        linha["_md5"] = hashlib.md5(w).hexdigest()
        chave = (linha["modalidade"], linha["nome"], linha["_md5"])
        areas.setdefault(
            chave,
            (*chave, linha["categoria"], linha["uso"], linha["jurisdicao"],
             linha["estado"], psycopg2.Binary(w)),
        )  # fmt: skip
    retorno = execute_values(
        cur,
        """INSERT INTO imazongeo.ap_area_protegida
               (modalidade, nome, geom_md5, categoria, uso, jurisdicao, estado, geom)
           VALUES %s
           ON CONFLICT (modalidade, nome, geom_md5) DO UPDATE SET
               categoria = EXCLUDED.categoria, uso = EXCLUDED.uso,
               jurisdicao = EXCLUDED.jurisdicao, estado = EXCLUDED.estado
           RETURNING modalidade, nome, geom_md5, id""",
        list(areas.values()),
        template=f"(%s, %s, %s, %s, %s, %s, %s, ST_GeomFromWKB(%s, {SRID}))",
        page_size=20,
        fetch=True,
    )
    ids = {(m, n, h): i for m, n, h, i in retorno}
    campos = ["ano", "trimestre", "legenda", "recorte", "classe", "posicao", "celulas"]
    execute_values(
        cur,
        f"""INSERT INTO imazongeo.ap_ranking
                (carga_id, area_id, {", ".join(campos)}, atributos) VALUES %s""",
        [
            (
                carga_id,
                ids[(linha["modalidade"], linha["nome"], linha.pop("_md5"))],
                *(linha[c] for c in campos),
                _json(p),
            )
            for linha, p in zip(envio.linhas, envio.atributos, strict=True)
        ],
    )


def _inserir_floreser(cur, carga_id: int, envio: Envio) -> None:
    campos = envio.padrao.nomes
    execute_values(
        cur,
        f"""INSERT INTO imazongeo.floreser_municipio (carga_id, {", ".join(campos)})
            VALUES %s""",
        [(carga_id, *(linha[c] for c in campos)) for linha in envio.linhas],
        page_size=2000,
    )


_INSERIR = {
    "simex": _inserir_simex,
    "ameaca_pressao": _inserir_ap,
    "floreser": _inserir_floreser,
}


def registrar_publicacao(
    cur,
    carga_id: int,
    formato: str,
    bucket: str,
    chave_s3: str,
    caminho: Path,
    sha256: str,
    publica: bool,
) -> None:
    cur.execute(
        """INSERT INTO imazongeo.publicacao
               (carga_id, formato, bucket, chave_s3, tamanho_bytes, sha256, publica)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (carga_id, formato, bucket, chave_s3, caminho.stat().st_size, sha256, publica),
    )


# ---------------------------------------------------------------------------
# Leitura no padrão
# ---------------------------------------------------------------------------


def ler_periodo(conn, p: Padrao, periodo: Periodo) -> pd.DataFrame:
    """Registros do período com os campos do padrão (GeoDataFrame se houver
    geometria), na ordem das visões ``vw_{dataset}``."""
    filtro = "ano = %s" + (" AND trimestre = %s" if periodo.trimestre else "")
    valores = (periodo.ano,) + ((periodo.trimestre,) if periodo.trimestre else ())
    colunas = ", ".join(p.nomes)
    geom = ", ST_AsBinary(geom) AS geom" if p.geometria else ""
    ordem = {
        "simex": "camada, id",
        "ameaca_pressao": "recorte, classe, posicao, id",
        "floreser": "cod_uf, cod_mun, idade",
    }[p.slug]
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {colunas}{geom} FROM imazongeo.vw_{p.slug} "
            f"WHERE {filtro} ORDER BY {ordem}",
            valores,
        )
        linhas = cur.fetchall()
    df = pd.DataFrame(linhas, columns=p.nomes + (["geom"] if p.geometria else []))
    for campo in p.campos:
        tipo = {"int": "Int64", "float": "float64", "str": "string"}[campo.tipo]
        df[campo.nome] = df[campo.nome].astype(tipo)
    if not p.geometria:
        return df
    geometria = shapely.from_wkb([bytes(g) for g in df.pop("geom")])
    return gpd.GeoDataFrame(df, geometry=geometria, crs=SRID)


def periodos_vigentes(conn, dataset: str) -> list[Periodo]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT ano, trimestre, mes FROM imazongeo.carga
               WHERE dataset = %s AND vigente ORDER BY ano, trimestre, mes""",
            (dataset,),
        )
        return [Periodo(a, t, m) for a, t, m in cur.fetchall()]
