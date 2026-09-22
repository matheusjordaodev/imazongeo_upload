"""SAD no banco: gravação dos alertas recebidos (tabela imazongeo.sad_alerta).

Cada arquivo do envio traz um tipo de alerta num recorte territorial (camada),
com todos os meses, ex.::

    alertas_sad_desmatamento_01_2008_07_2026_municipios.geojson

Cada mês de cada tipo/camada é uma carga (``particao`` = ``tipo/camada``). O
envio substitui, para o tipo/camada do arquivo, todos os meses do intervalo do
nome (01/2008 a 07/2026 no exemplo), inclusive meses sem alertas, e os meses
presentes nos dados. As outras partes (outros tipos e camadas) não mudam.

Os arquivos grandes (mais de 1 GB) são lidos em blocos. As correções de
``correcao.py`` valem também aqui: textos, grafias, geometrias inválidas,
registros repetidos no mesmo envio e área (a do arquivo é mantida, exceto se
estiver vazia ou diferir mais de 1% da área do polígono).

Modos: ``dry_run`` só lê e valida; ``simulation`` grava numa transação desfeita
no final; ``real`` grava (uma transação para o envio inteiro).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import geopandas as gpd
import psycopg2
import pyogrio
import shapely
from psycopg2.extras import execute_values

from ..sad import CamadaSAD, _extrair_zips_sad, identificar_camada_sad
from . import correcao as cor
from . import db
from . import normalizacao as norm
from .entrada import geometrias_wkb, sha256_arquivo
from .padrao import SAD, SRID

# Camada no nome do arquivo (ver sad._SAD_CAMADAS) → camada do padrão
CAMADAS = {
    "amazoniaLegal": "amazonia_legal",
    "municipios": "municipios",
    "assentamentos": "assentamentos",
    "terraIndigena": "terras_indigenas",
    "unidadeConservacao": "unidades_conservacao",
}
LOTE = 20000  # feições lidas e gravadas por vez
_CAMPOS = [c for c in SAD.nomes if c not in ("tipo", "camada")]


@dataclass
class ArquivoSAD:
    caminho: Path
    tipo: str  # 'desmatamento' | 'degradacao'
    camada: str  # camada do padrão (CAMADAS_SAD)
    meses: list[tuple[int, int]]  # (ano, mês) do intervalo do nome do arquivo

    @property
    def particao(self) -> str:
        return f"{self.tipo}/{self.camada}"


@dataclass
class Resultado:
    registros: Counter = field(default_factory=Counter)  # por particao
    meses: Counter = field(default_factory=Counter)  # meses substituídos por particao
    avisos: list[str] = field(default_factory=list)


def intervalo(inicio: tuple[int, int], fim: tuple[int, int]) -> list[tuple[int, int]]:
    """Meses (ano, mês) de ``inicio`` a ``fim``, inclusive."""
    ano, mes = inicio
    meses = []
    while (ano, mes) <= fim:
        meses.append((ano, mes))
        ano, mes = (ano + 1, 1) if mes == 12 else (ano, mes + 1)
    return meses


def identificar(caminho: Path, tipo: str | None = None, camada: str | None = None):
    """Tipo, camada e meses pelo nome do arquivo (ou pelos informados)."""
    info = identificar_camada_sad(caminho.name)
    if info:
        tipo_nome, camada_nome, periodos = info
        return ArquivoSAD(
            caminho,
            tipo_nome,
            CAMADAS[camada_nome],
            intervalo(min(periodos), max(periodos)),
        )
    if tipo and camada:
        return ArquivoSAD(caminho, tipo, camada, [])
    raise ValueError(
        f"{caminho.name}: nome fora do padrão do SAD "
        "(alertas_sad_{tipo}_{MM}_{AAAA}_{MM}_{AAAA}_{camada})."
    )


def arquivos_de(caminhos: list[Path], pasta: Path) -> list[ArquivoSAD]:
    """Arquivos do envio: ZIPs (extraídos em ``pasta``), GeoJSONs ou Shapefiles."""
    zips = [c for c in caminhos if c.suffix.lower() == ".zip"]
    soltos = [c for c in caminhos if c.suffix.lower() in (".geojson", ".shp")]
    outros = set(caminhos) - set(zips) - set(soltos)
    if outros:
        raise ValueError(f"Arquivo não suportado: {sorted(outros)[0].name}")
    arquivos = [identificar(c) for c in soltos]
    if zips:
        arquivos += [arquivo_da_camada(c) for c in _extrair_zips_sad(zips, pasta)]
    if not arquivos:
        raise ValueError("Nenhum arquivo do SAD encontrado.")
    return arquivos


def arquivo_da_camada(c: CamadaSAD) -> ArquivoSAD:
    """Converte a camada já extraída por sad.processar_sad_zip."""
    return identificar(c.path, c.tipo, CAMADAS[c.camada])


def ler_lotes(caminho: Path) -> Iterator[gpd.GeoDataFrame]:
    """Feições do arquivo em blocos de ``LOTE``, sem carregar tudo na memória."""
    with pyogrio.open_arrow(caminho, batch_size=LOTE, use_pyarrow=True) as (
        meta,
        leitor,
    ):
        coluna = meta["geometry_name"] or "wkb_geometry"
        for lote in leitor:
            df = lote.to_pandas()
            geometria = shapely.from_wkb(df.pop(coluna).to_numpy())
            yield gpd.GeoDataFrame(df, geometry=geometria, crs=meta["crs"])


def _preparar_lote(
    gdf: gpd.GeoDataFrame, arq: ArquivoSAD, contador: cor.Contador, primeira: int
) -> tuple[list[dict], list[dict], list[bytes]]:
    """Registros no padrão, atributos originais e geometrias (WKB) do lote."""
    nome = arq.caminho.name
    props = norm.propriedades(gdf)
    linhas = []
    for i, p in enumerate(props):
        limpo = {
            k: cor.limpar_texto(v, contador) if isinstance(v, str) else v
            for k, v in p.items()
        }
        try:
            linhas.append(norm.linha_sad(limpo, arq.tipo, arq.camada))
        except (ValueError, TypeError) as e:
            raise ValueError(f"{nome}, registro {primeira + i + 1}: {e}") from None
    wkbs = geometrias_wkb(gdf, nome, contador)
    for linha, w in zip(linhas, wkbs, strict=True):
        if linha["uf"]:
            linha["uf"] = linha["uf"].upper()
        linha["uso"] = cor.padronizar(linha["uso"], cor.USOS_AP, contador)
        linha["jurisdicao"] = cor.padronizar(
            linha["jurisdicao"], cor.JURISDICOES_AP, contador
        )
        area = cor.area_ha(shapely.from_wkb(w)) / 100
        if linha["area_km2"] is None or abs(linha["area_km2"] - area) > 0.01 * area:
            contador["área(s) vazia(s) ou diferente(s) do polígono recalculada(s)"] += 1
            linha["area_km2"] = round(area, 6)
    return linhas, props, wkbs


class _Gravacao:
    """Cargas mensais de uma parte (tipo/camada) numa transação aberta."""

    def __init__(self, cur, arq: ArquivoSAD, sha256: str, tamanho: int) -> None:
        self.cur, self.arq = cur, arq
        self.sha256, self.tamanho = sha256, tamanho
        self.cargas: dict[tuple[int, int], int] = {}
        self.registros: Counter = Counter()
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"imazongeo:sad:{arq.particao}",),
        )

    def carga(self, ano: int, mes: int) -> int:
        """Carga nova do mês (a anterior deixa de valer e seus alertas saem)."""
        if (ano, mes) in self.cargas:
            return self.cargas[(ano, mes)]
        cur = self.cur
        cur.execute(
            """UPDATE imazongeo.carga SET vigente = false, substituida_em = now()
               WHERE dataset = 'sad' AND vigente AND ano = %s AND mes = %s
                 AND particao = %s RETURNING id""",
            (ano, mes, self.arq.particao),
        )
        anteriores = [r[0] for r in cur.fetchall()]
        if anteriores:
            cur.execute(
                "DELETE FROM imazongeo.sad_alerta WHERE carga_id = ANY(%s)",
                (anteriores,),
            )
        cur.execute(
            """INSERT INTO imazongeo.carga (dataset, ano, mes, particao, origem,
                   arquivo, sha256, tamanho_bytes, registros)
               VALUES ('sad', %s, %s, %s, 'envio', %s, %s, %s, 0) RETURNING id""",
            (
                ano,
                mes,
                self.arq.particao,
                self.arq.caminho.name,
                self.sha256,
                self.tamanho,
            ),
        )
        self.cargas[(ano, mes)] = cur.fetchone()[0]
        return self.cargas[(ano, mes)]

    def inserir(self, linhas: list[dict], props: list[dict], wkbs: list[bytes]) -> None:
        valores = []
        for linha, p, w in zip(linhas, props, wkbs, strict=True):
            carga_id = self.carga(linha["ano"], linha["mes"])
            self.registros[carga_id] += 1
            valores.append(
                (
                    carga_id,
                    self.arq.tipo,
                    self.arq.camada,
                    *(linha[c] for c in _CAMPOS),
                    json.dumps(p, ensure_ascii=False, default=str),
                    psycopg2.Binary(w),
                )
            )
        execute_values(
            self.cur,
            f"""INSERT INTO imazongeo.sad_alerta (carga_id, tipo, camada,
                    {", ".join(_CAMPOS)}, atributos, geom) VALUES %s""",
            valores,
            template=f"({', '.join(['%s'] * (len(_CAMPOS) + 4))}::jsonb,"
            f" ST_GeomFromWKB(%s, {SRID}))",
            page_size=1000,
        )

    def fechar(self) -> None:
        if self.cargas:
            execute_values(
                self.cur,
                """UPDATE imazongeo.carga c SET registros = v.n
                   FROM (VALUES %s) AS v(id, n) WHERE c.id = v.id""",
                [(i, self.registros[i]) for i in self.cargas.values()],
            )


def _processar(arq: ArquivoSAD, cur, resultado: Resultado) -> None:
    """Lê o arquivo em blocos e, com ``cur``, grava cada mês (senão só valida)."""
    contador = cor.Contador()
    gravacao = None
    if cur is not None:
        gravacao = _Gravacao(
            cur, arq, sha256_arquivo(arq.caminho), arq.caminho.stat().st_size
        )
        for ano, mes in arq.meses:  # meses sem alertas também são substituídos
            gravacao.carga(ano, mes)
    vistos: set[bytes] = set()
    meses_dados: set[tuple[int, int]] = set()
    lidos = 0
    for gdf in ler_lotes(arq.caminho):
        linhas, props, wkbs = _preparar_lote(gdf, arq, contador, lidos)
        lidos += len(linhas)
        manter = []
        for linha, w in zip(linhas, wkbs, strict=True):
            chave = hashlib.md5(
                repr([linha[c] for c in _CAMPOS]).encode() + w, usedforsecurity=False
            ).digest()
            manter.append(chave not in vistos)
            vistos.add(chave)
        contador["registro(s) repetido(s) removido(s)"] += manter.count(False)
        sel = [i for i, ok in enumerate(manter) if ok]
        linhas = [linhas[i] for i in sel]
        meses_dados.update((x["ano"], x["mes"]) for x in linhas)
        if gravacao is not None:
            gravacao.inserir(linhas, [props[i] for i in sel], [wkbs[i] for i in sel])
        resultado.registros[arq.particao] += len(linhas)
    fora = meses_dados - set(arq.meses)
    if arq.meses and fora:
        resultado.avisos.append(
            f"{arq.caminho.name}: {len(fora)} mês(es) fora do intervalo do nome "
            f"(ex.: {min(fora)[1]:02d}/{min(fora)[0]})"
        )
    if gravacao is not None:
        gravacao.fechar()
    resultado.meses[arq.particao] += len(set(arq.meses) | meses_dados)
    resultado.avisos += contador.avisos(arq.caminho.name)
    logging.info(
        "SAD %s: %d alerta(s) em %d mês(es) de %s",
        arq.particao,
        resultado.registros[arq.particao],
        len(set(arq.meses) | meses_dados),
        arq.caminho.name,
    )


def gravar(
    arquivos: list[ArquivoSAD], modo: str = "dry_run", database_url: str | None = None
) -> Resultado:
    """Grava os arquivos do envio (uma transação para todos)."""
    if modo not in ("dry_run", "simulation", "real"):
        raise ValueError(f"Modo inválido: {modo}")
    particoes = Counter(a.particao for a in arquivos)
    repetidas = [p for p, n in particoes.items() if n > 1]
    if repetidas:
        raise ValueError(f"Mais de um arquivo para {repetidas[0]} no mesmo envio.")
    resultado = Resultado()
    if modo == "dry_run":
        for arq in arquivos:
            _processar(arq, None, resultado)
        logging.info("[PRÉVIA] Nada foi gravado no banco.")
    else:
        with db.conectar(database_url) as conn:
            try:
                with conn.cursor() as cur:
                    for arq in arquivos:
                        _processar(arq, cur, resultado)
                if modo == "simulation":
                    conn.rollback()
                    logging.info("[SIMULAÇÃO] Transação desfeita: o banco não mudou.")
                else:
                    conn.commit()
                    logging.info(
                        "SAD gravado no banco: %d alerta(s).",
                        sum(resultado.registros.values()),
                    )
            except Exception:
                conn.rollback()
                raise
    for aviso in resultado.avisos:
        logging.warning("%s", aviso)
    return resultado


def gravar_caminhos(
    caminhos: list[Path], modo: str = "dry_run", database_url: str | None = None
) -> Resultado:
    """Grava ZIPs, GeoJSONs ou Shapefiles do SAD (comando imazongeo-banco)."""
    with tempfile.TemporaryDirectory(prefix="imazon_sad_banco_") as tmp:
        return gravar(arquivos_de(caminhos, Path(tmp)), modo, database_url)


def modo_banco(dry_run: bool, simulacao: bool = False) -> str | None:
    """Modo de gravação no banco para o fluxo do SAD (None = não gravar).

    Sem DATABASE_URL, o SAD segue só para o S3, com um aviso.
    """
    if dry_run:
        return None
    if not os.getenv("DATABASE_URL", "").strip():
        logging.warning(
            "DATABASE_URL não configurada: o SAD não será gravado no banco."
        )
        return None
    return "simulation" if simulacao else "real"
