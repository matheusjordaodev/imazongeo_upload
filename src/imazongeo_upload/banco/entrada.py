"""Leitura e validação do arquivo enviado, antes de qualquer gravação.

O envio é um ZIP com GeoJSONs ou Shapefiles (todas as camadas do período),
ou um único GeoJSON. Para o Floreser, que não tem geometria, CSV também é
aceito. O resultado (:class:`Envio`) tem os registros já no padrão.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import numpy as np
import pandas as pd
import pyogrio
import shapely
from shapely.geometry import MultiPolygon

from . import correcao as cor
from . import normalizacao as norm
from .padrao import (
    AMEACA_PRESSAO,
    CAMADAS_SIMEX,
    CLASSES_AP,
    FLORESER,
    RECORTES_AP,
    SIMEX,
    SRID,
    Padrao,
    Periodo,
    padrao,
)

EXT_GEO = (".geojson", ".shp")


@dataclass
class Envio:
    padrao: Padrao
    periodo: Periodo
    arquivo: str  # nome do arquivo recebido (ou chave S3 legada)
    sha256: str
    tamanho_bytes: int
    linhas: list[dict]  # campos do padrão
    atributos: list[dict]  # propriedades originais
    wkbs: list[bytes] | None  # geometrias (None no Floreser)
    avisos: list[str] = field(default_factory=list)

    @property
    def registros(self) -> int:
        return len(self.linhas)

    def resumo(self) -> str:
        return (
            f"{self.padrao.slug} {self.periodo}: {self.registros} registros "
            f"de {self.arquivo}"
        )


def sha256_arquivo(caminho: Path) -> str:
    h = hashlib.sha256()
    with open(caminho, "rb") as f:
        for bloco in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloco)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Arquivos
# ---------------------------------------------------------------------------


def _extrair_zip(caminho: Path, destino: Path) -> list[Path]:
    """Extrai os arquivos do ZIP (sem pastas), recusando caminhos inseguros."""
    extraidos = []
    destino.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(caminho) as zf:
        for item in zf.infolist():
            nome = PurePosixPath(item.filename.replace("\\", "/"))
            if nome.is_absolute() or ".." in nome.parts or ":" in item.filename:
                raise ValueError("O ZIP contém um caminho inválido.")
            if item.is_dir() or nome.name.startswith(("._", ".")):
                continue
            alvo = destino / nome.name
            if alvo.exists():
                raise ValueError(f"Nome de arquivo repetido no ZIP: {nome.name}")
            with zf.open(item) as origem, open(alvo, "wb") as saida:
                shutil.copyfileobj(origem, saida)
            extraidos.append(alvo)
    return extraidos


def ler_camadas(caminho: Path, pasta: Path, aceita_csv: bool = False) -> list[tuple]:
    """Lista de (nome do arquivo, DataFrame/GeoDataFrame) do envio."""
    ext = caminho.suffix.lower()
    if ext == ".zip":
        arquivos = _extrair_zip(caminho, pasta)
    elif ext in (".geojson", ".json") or (aceita_csv and ext == ".csv"):
        arquivos = [caminho]
    else:
        raise ValueError("Envie um .zip (com GeoJSON ou Shapefile) ou um .geojson.")

    por_ext = {e: [a for a in arquivos if a.suffix.lower() == e] for e in EXT_GEO}
    if ext in (".geojson", ".json"):
        por_ext[".geojson"] = arquivos
    csvs = [a for a in arquivos if a.suffix.lower() == ".csv"] if aceita_csv else []
    usados = [e for e, lista in por_ext.items() if lista] + ([".csv"] if csvs else [])
    if not usados:
        tipos = "GeoJSON, Shapefile ou CSV" if aceita_csv else "GeoJSON ou Shapefile"
        raise ValueError(f"Nenhum {tipos} encontrado no envio.")
    if len(usados) > 1:
        raise ValueError("Envie as camadas em um único formato (GeoJSON ou Shapefile).")

    camadas = []
    for arq in sorted(por_ext[".geojson"] + por_ext[".shp"]):
        df = pyogrio.read_dataframe(arq)
        if df.empty:
            raise ValueError(f"{arq.name} não tem feições.")
        camadas.append((arq.name, df))
    for arq in sorted(csvs):
        df = pd.read_csv(arq, dtype=str, keep_default_na=False, encoding="utf-8-sig")
        if df.empty:
            raise ValueError(f"{arq.name} não tem registros.")
        camadas.append((arq.name, df))
    return camadas


def reparar(g, contador: cor.Contador | None = None):
    """Corrige uma geometria inválida com ``make_valid``.

    Nos dados publicados, o caso comum é o anel que toca a si mesmo num ponto
    (válido no ArcGIS, inválido no padrão OGC): o reparo separa as partes do
    polígono sem alterar a área. Linhas e pontos que o reparo gerar (partes
    sem área) são descartados; se não sobrar polígono, é erro.
    """
    partes = shapely.get_parts(shapely.make_valid(g))
    poligonos = []
    for parte in partes:
        if parte.geom_type == "Polygon":
            poligonos.append(parte)
        elif parte.geom_type == "MultiPolygon":
            poligonos.extend(parte.geoms)
    if not poligonos:
        raise ValueError("geometria inválida sem área (só linhas ou pontos)")
    if len(partes) > len(poligonos) and contador is not None:
        contador["geometria(s) com linhas/pontos sem área descartados no reparo"] += 1
    corrigida = MultiPolygon(poligonos)
    if not corrigida.is_valid:
        raise ValueError("geometria continua inválida após o reparo")
    return corrigida


def geometrias_wkb(
    gdf, nome: str = "", contador: cor.Contador | None = None
) -> list[bytes]:
    """Geometrias em EPSG:4326, 2D, válidas e MultiPolygon, como WKB.

    Geometrias inválidas são corrigidas (:func:`reparar`) e contadas.
    """
    if gdf.crs is None:
        raise ValueError("Arquivo sem sistema de coordenadas (CRS).")
    if gdf.crs.to_epsg() != SRID:
        gdf = gdf.to_crs(SRID)
    saida = []
    prefixo = f"{nome}, " if nome else ""
    for i, g in enumerate(shapely.force_2d(np.asarray(gdf.geometry.array))):
        if g is None or g.is_empty:
            raise ValueError(f"{prefixo}feição {i + 1} sem geometria")
        if g.geom_type not in ("Polygon", "MultiPolygon"):
            raise ValueError(
                f"{prefixo}feição {i + 1} com geometria {g.geom_type}; "
                "esperado polígono"
            )
        if not g.is_valid:
            try:
                g = reparar(g, contador)
            except ValueError as e:
                raise ValueError(f"{prefixo}feição {i + 1}: {e}") from None
            if contador is not None:
                contador["geometria(s) inválida(s) corrigida(s)"] += 1
        if g.geom_type == "Polygon":
            g = MultiPolygon([g])
        saida.append(shapely.to_wkb(g))
    return saida


# ---------------------------------------------------------------------------
# Preparação por dataset
# ---------------------------------------------------------------------------


def _normalizar(
    nome: str, props: list[dict], contador: cor.Contador, funcao, *args
) -> list[dict]:
    """Registros no padrão, com os textos já corrigidos (codificação, espaços)."""
    linhas = []
    for i, p in enumerate(props):
        limpo = {
            k: cor.limpar_texto(v, contador) if isinstance(v, str) else v
            for k, v in p.items()
        }
        try:
            linhas.append(funcao(limpo, *args))
        except (ValueError, TypeError) as e:
            raise ValueError(f"{nome}, registro {i + 1}: {e}") from None
    return linhas


def _conferir_ano(linhas: list[dict], periodo: Periodo, nome: str) -> None:
    """Registros sem ano recebem o do período; com outro ano, é erro."""
    anos = Counter(linha["ano"] for linha in linhas if linha["ano"] is not None)
    outros = {a: n for a, n in anos.items() if a != periodo.ano}
    if outros:
        raise ValueError(
            f"{nome}: registros de outro(s) ano(s) {outros}; o período informado "
            f"é {periodo}."
        )
    for linha in linhas:
        linha["ano"] = periodo.ano


def _remover_repetidos(
    chaves: list, contador: cor.Contador, *listas: list | None
) -> None:
    """Remove registros idênticos a um anterior do mesmo envio (mesmo período)."""
    repetidos = cor.indices_repetidos(chaves)
    for i in reversed(repetidos):
        for lista in listas:
            if lista is not None:
                del lista[i]
    contador["registro(s) repetido(s) removido(s)"] += len(repetidos)


def _simex(camadas: list[tuple], periodo: Periodo) -> tuple:
    linhas, atributos, wkbs, avisos = [], [], [], []
    for nome, gdf in camadas:
        contador = cor.Contador()
        props = norm.propriedades(gdf)
        novas = _normalizar(
            nome, props, contador, norm.linha_simex, norm.camada_simex(nome, False)
        )
        _conferir_ano(novas, periodo, nome)
        geoms = geometrias_wkb(gdf, nome, contador)
        for linha, w in zip(novas, geoms, strict=True):
            if linha["uf"]:
                linha["uf"] = linha["uf"].upper()
            # A área vem da geometria: no arquivo, as camadas fundiárias
            # trazem a área do polígono antes do recorte.
            area = round(cor.area_ha(shapely.from_wkb(w)), 6)
            if linha["area_ha"] is None or abs(area - linha["area_ha"]) > 0.01 * area:
                contador["área(s) recalculada(s) pela geometria (diferença > 1%)"] += 1
            linha["area_ha"] = area
        chaves = [
            (tuple(linha[c] for c in SIMEX.nomes if c != "area_ha"), w)
            for linha, w in zip(novas, geoms, strict=True)
        ]
        _remover_repetidos(chaves, contador, novas, props, geoms)
        linhas += novas
        atributos += props
        wkbs += geoms
        avisos += contador.avisos(nome)
    faltando = set(CAMADAS_SIMEX) - {linha["camada"] for linha in linhas}
    if faltando:
        raise ValueError(
            "O envio do SIMEX deve ter todas as camadas do ano. Faltando: "
            + ", ".join(sorted(faltando))
        )
    norm.completar_cod_mun(linhas)
    return linhas, atributos, wkbs, avisos


def _padronizar_ap(linha: dict, contador: cor.Contador) -> None:
    linha["uso"] = cor.padronizar(linha["uso"], cor.USOS_AP, contador)
    linha["categoria"] = cor.padronizar(linha["categoria"], cor.CATEGORIAS_AP, contador)
    linha["jurisdicao"] = cor.padronizar(
        linha["jurisdicao"], cor.JURISDICOES_AP, contador
    )
    linha["legenda"] = cor.padronizar_palavras(linha["legenda"], cor.MESES, contador)
    if linha["estado"]:
        linha["estado"] = linha["estado"].upper()


def _ameaca_pressao(camadas: list[tuple], periodo: Periodo) -> tuple:
    linhas, atributos, wkbs, avisos = [], [], [], []
    contador = cor.Contador()
    for nome, gdf in camadas:
        props = norm.propriedades(gdf)
        novas = _normalizar(nome, props, contador, norm.linha_ap, norm.grupo_ap(nome))
        geoms = geometrias_wkb(gdf, nome, contador)
        # Arquivos anuais por categoria trazem vários trimestres: fica só o
        # período informado. Registros sem ano/trimestre são desse período.
        manter = [
            linha["ano"] in (None, periodo.ano)
            and linha["trimestre"] in (None, periodo.trimestre)
            for linha in novas
        ]
        ignorados = manter.count(False)
        if ignorados:
            avisos.append(
                f"{nome}: {ignorados} registro(s) de outros períodos ignorados"
            )
        for linha, p, w, ok in zip(novas, props, geoms, manter, strict=True):
            if ok:
                linha["ano"], linha["trimestre"] = periodo.ano, periodo.trimestre
                _padronizar_ap(linha, contador)
                linhas.append(linha)
                atributos.append(p)
                wkbs.append(w)
    if not linhas:
        raise ValueError(f"Nenhum registro de Ameaça & Pressão para {periodo}.")
    chaves = [
        (tuple(linha[c] for c in AMEACA_PRESSAO.nomes), w)
        for linha, w in zip(linhas, wkbs, strict=True)
    ]
    _remover_repetidos(chaves, contador, linhas, atributos, wkbs)
    perdidos = sum(cor.PERDIDO in (linha["nome"] or "") for linha in linhas)
    if perdidos:
        contador["nome(s) com letra perdida (�), comparados com o banco ao gravar"] += (
            perdidos
        )
    grupos = {(linha["recorte"], linha["classe"]) for linha in linhas}
    faltando = {(r, c) for r in RECORTES_AP for c in CLASSES_AP} - grupos
    if faltando:
        raise ValueError(
            f"O envio de {periodo} deve ter todos os rankings (recorte × classe). "
            "Faltando: " + ", ".join(f"{r}/{c}" for r, c in sorted(faltando))
        )
    chaves_area = Counter(
        (linha["recorte"], linha["classe"], linha["modalidade"], linha["nome"])
        for linha in linhas
    )
    repetidas = [k for k, n in chaves_area.items() if n > 1]
    if repetidas:
        raise ValueError(
            f"Área repetida no mesmo ranking com dados diferentes: {repetidas[0]}"
        )
    return linhas, atributos, wkbs, avisos + contador.avisos(periodo.sufixo)


def _floreser(camadas: list[tuple], periodo: Periodo) -> tuple:
    linhas, atributos, avisos = [], [], []
    contador = cor.Contador()
    for nome, df in camadas:
        if "geometry" in df.columns:
            avisos.append(f"{nome}: geometria ignorada (o Floreser é tabular)")
            df = pd.DataFrame(df.drop(columns="geometry"))
        props = norm.propriedades(df)
        novas = _normalizar(nome, props, contador, norm.linha_floreser)
        _conferir_ano(novas, periodo, nome)
        for linha in novas:
            linha["estado"] = cor.padronizar(linha["estado"], cor.ESTADOS, contador)
        linhas += novas
        atributos += props
    chaves = [tuple(linha[c] for c in FLORESER.nomes) for linha in linhas]
    _remover_repetidos(chaves, contador, linhas, atributos)
    contagem = Counter((r["cod_uf"], r["cod_mun"], r["idade"]) for r in linhas)
    repetidas = [k for k, n in contagem.items() if n > 1]
    if repetidas:
        raise ValueError(
            f"{len(repetidas)} registro(s) de UF/município/idade repetido(s) com "
            f"áreas diferentes, ex.: {repetidas[0]}"
        )
    return linhas, atributos, None, avisos + contador.avisos(periodo.sufixo)


_PREPARADORES = {
    "simex": _simex,
    "ameaca_pressao": _ameaca_pressao,
    "floreser": _floreser,
}


def preparar_envio(
    caminho: Path, dataset: str, periodo: Periodo, nome: str | None = None
) -> Envio:
    """Lê, valida e converte o envio para o padrão. Não grava nada."""
    p = padrao(dataset)
    p.validar_periodo(periodo)
    with tempfile.TemporaryDirectory(prefix="imazon_envio_") as tmp:
        camadas = ler_camadas(caminho, Path(tmp), aceita_csv=not p.geometria)
        linhas, atributos, wkbs, avisos = _PREPARADORES[dataset](camadas, periodo)
    envio = Envio(
        padrao=p,
        periodo=periodo,
        arquivo=nome or caminho.name,
        sha256=sha256_arquivo(caminho),
        tamanho_bytes=caminho.stat().st_size,
        linhas=linhas,
        atributos=atributos,
        wkbs=wkbs,
        avisos=avisos,
    )
    for aviso in avisos:
        logging.warning("%s", aviso)
    logging.info("Envio validado: %s", envio.resumo())
    return envio
