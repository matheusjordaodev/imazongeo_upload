"""Conversão dos registros recebidos para os campos do padrão (padrao.py).

Aceita os nomes de campo do padrão e as variações encontradas nos arquivos
publicados desde 2007 (ex.: SIMEX ``sigla_uf``/``UF``, ``source_layer``/``fonte``;
A&P ``periodo``/``trimestre``; Floreser ``CD_GEOCUF``, ``IDADE``). Os nomes são
comparados sem diferenciar maiúsculas de minúsculas.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .padrao import (
    CAMADAS_SIMEX,
    CATEGORIAS_SIMEX,
    CLASSES_AP,
    MODALIDADES_AP,
    RECORTES_AP,
)


def valor_json(v: Any) -> Any:
    """Converte valores do pandas/numpy em tipos serializáveis (NaN -> None)."""
    if v is None or v is pd.NA or v is pd.NaT:
        return None
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, datetime | date):
        return v.isoformat()
    return v


def propriedades(df: pd.DataFrame) -> list[dict]:
    """Registros do DataFrame (sem a geometria) com valores serializáveis."""
    cols = [c for c in df.columns if c != "geometry"]
    return [
        {c: valor_json(v) for c, v in zip(cols, linha, strict=True)}
        for linha in df[cols].itertuples(index=False, name=None)
    ]


def _minusculas(p: dict) -> dict:
    """Chaves em minúsculas; havendo repetição (ano/Ano), vale a preenchida."""
    saida: dict = {}
    for k, v in p.items():
        k = str(k).lower()
        if k not in saida or saida[k] is None:
            saida[k] = v
    return saida


def _primeiro(p: dict, *chaves: str) -> Any:
    for chave in chaves:
        v = p.get(chave)
        if v is not None and not (isinstance(v, str) and not v.strip()):
            return v
    return None


def _int(v: Any) -> int | None:
    v = valor_json(v)
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    return int(float(v))


def _float(v: Any) -> float | None:
    v = valor_json(v)
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    return float(v)


def _texto(v: Any) -> str | None:
    v = valor_json(v)
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def _exigir(linha: dict, *campos: str) -> dict:
    faltando = [c for c in campos if linha.get(c) is None]
    if faltando:
        raise ValueError(f"campo(s) obrigatório(s) vazio(s): {', '.join(faltando)}")
    return linha


# ---------------------------------------------------------------------------
# SIMEX
# ---------------------------------------------------------------------------

# Variações do nome da camada (atributo source_layer/fonte/camada ou nome do
# arquivo, ex.: simex_amz_2024_imoveisruraisprivados(1).geojson). A ordem
# importa: prefixos mais longos antes dos curtos.
_CAMADAS_SIMEX = (
    ("imoveis_rurais_privados", "imoveis_rurais"),
    ("imoveis_rurais", "imoveis_rurais"),
    ("imoveisrurais", "imoveis_rurais"),
    ("municipios", "municipios"),
    ("mun", "municipios"),
    ("assentamentos", "assentamentos"),
    ("terras_publicas_nao_destinadas", "terras_nao_destinadas"),
    ("terras_nao_destinadas", "terras_nao_destinadas"),
    ("terraspndest", "terras_nao_destinadas"),
    ("terrasndest", "terras_nao_destinadas"),
    ("terras_ndest", "terras_nao_destinadas"),
    ("terras_indigenas", "terras_indigenas"),
    ("ti", "terras_indigenas"),
    ("unidades_conservacao", "unidades_conservacao"),
    ("uc", "unidades_conservacao"),
)
_CATEGORIAS_SIMEX = {
    "autorizada": "autorizada",
    "não autorizada": "não autorizada",
    "nao autorizada": "não autorizada",
    "análise": "análise",
    "analise": "análise",
    "em análise": "análise",
}
assert set(_CATEGORIAS_SIMEX.values()) == set(CATEGORIAS_SIMEX)
assert {c for _, c in _CAMADAS_SIMEX} == set(CAMADAS_SIMEX)


def camada_simex(texto: str | None, exato: bool = True) -> str | None:
    """Camada do padrão a partir do valor do atributo ou do nome do arquivo.

    Com ``exato=False`` (nomes de arquivo), remove o prefixo simex_amz_AAAA_,
    a extensão e sufixos como "(2)" e aceita o nome como prefixo.
    """
    if not texto:
        return None
    token = str(texto).strip().lower()
    if not exato:
        token = Path(token).stem
        token = re.sub(r"^simex_(?:amz|amazonia)_(?:pamtm?_)?(?:\d{4}_)?", "", token)
        token = re.sub(r"\(\d+\)$", "", token).strip("_ ")
    for variacao, camada in _CAMADAS_SIMEX:
        if token == variacao or (not exato and token.startswith(variacao)):
            return camada
    return None


def linha_simex(p: dict, camada_arquivo: str | None = None) -> dict:
    """Registro do SIMEX no padrão. ``camada_arquivo`` vem do nome do arquivo."""
    q = _minusculas(p)
    origem = _primeiro(q, "camada", "source_layer", "source_lay", "fonte")
    camada = camada_simex(origem) or camada_arquivo
    if camada is None:
        raise ValueError(f"camada SIMEX não identificada ({origem!r})")

    categoria = _texto(q.get("categoria"))  # ausente em todo o SIMEX 2020
    if categoria is not None:
        if categoria.lower() not in _CATEGORIAS_SIMEX:
            raise ValueError(f"categoria SIMEX desconhecida: {categoria!r}")
        categoria = _CATEGORIAS_SIMEX[categoria.lower()]

    # Até 2023, 'nome'/'geocodigo' são do município; a partir de 2024 o
    # município vem em NM_MUN/cd_mun e 'nome' é o nome da UC.
    tem_municipio = any(k in q for k in ("municipio", "nm_mun"))
    municipio = _primeiro(q, "municipio", "nm_mun") if tem_municipio else q.get("nome")
    if "territorio" in q:
        territorio = q["territorio"]
    elif camada == "municipios":
        territorio = None
    elif camada == "terras_indigenas":
        territorio = q.get("terrai_nom")
    elif camada == "unidades_conservacao":
        territorio = q["nome_1"] if "nome_1" in q else q.get("nome")
    else:
        territorio = q.get("name")

    return _exigir(
        {
            "ano": _int(_primeiro(q, "ano")),
            "camada": camada,
            "categoria": categoria,
            "uf": _texto(_primeiro(q, "uf", "sigla_uf")),
            "municipio": _texto(municipio),
            "cod_mun": _int(_primeiro(q, "cod_mun", "geocodigo", "cd_mun")),
            "territorio": _texto(territorio),
            "subclasse": _texto(_primeiro(q, "subclasse", "sub_class")),
            # Recalculada pela geometria na entrada (entrada.py)
            "area_ha": _float(q.get("area_ha")),
        },
    )


def completar_cod_mun(linhas: list[dict]) -> None:
    """Preenche o código IBGE ausente (TI/UC em 2024) por UF + município."""
    codigos = {
        (linha["uf"], linha["municipio"]): linha["cod_mun"]
        for linha in linhas
        if linha["cod_mun"] and linha["municipio"]
    }
    for linha in linhas:
        if linha["cod_mun"] is None:
            linha["cod_mun"] = codigos.get((linha["uf"], linha["municipio"]))


# ---------------------------------------------------------------------------
# Ameaça & Pressão
# ---------------------------------------------------------------------------

_GRUPO_AP_RE = re.compile(
    r"(?:^|_)(geral|categ_ti|categ_uce|categ_ucf)_(ameaca|pressao)$", re.IGNORECASE
)


def grupo_ap(nome_arquivo: str) -> tuple[str, str] | None:
    """(recorte, classe) pelo nome, ex.: ameaca_e_pressao_2025_categ_ti_ameaca."""
    m = _GRUPO_AP_RE.search(Path(nome_arquivo).stem)
    return (m.group(1).lower(), m.group(2).lower()) if m else None


def linha_ap(p: dict, grupo_arquivo: tuple[str, str] | None = None) -> dict:
    """Registro de Ameaça & Pressão no padrão.

    O trimestre vem de ``trimestre``, ``periodo`` (até 2023) ou do mês. Sem
    recorte/classe nos atributos, usa ``grupo_arquivo`` (nome do arquivo).
    """
    q = _minusculas(p)
    trimestre = _int(_primeiro(q, "trimestre", "periodo"))
    if trimestre is None and _primeiro(q, "mes") is not None:
        trimestre = (_int(q["mes"]) - 1) // 3 + 1
    recorte = _texto(_primeiro(q, "recorte", "dado"))
    classe = _texto(_primeiro(q, "classe", "class"))
    if grupo_arquivo and recorte is None and classe is None:
        recorte, classe = grupo_arquivo
    linha = {
        "ano": _int(_primeiro(q, "ano")),
        "trimestre": trimestre,
        "legenda": _texto(_primeiro(q, "legenda", "legend")),
        "recorte": recorte.lower() if recorte else None,
        "classe": classe.lower() if classe else None,
        "posicao": _int(_primeiro(q, "posicao", "rank")),
        "celulas": _int(q.get("celulas")),
        "nome": _texto(q.get("nome")),
        "modalidade": (_texto(q.get("modalidade")) or "").lower() or None,
        "categoria": _texto(q.get("categoria")),
        "uso": _texto(q.get("uso")),
        "jurisdicao": _texto(q.get("jurisdicao")),
        "estado": _texto(q.get("estado")),
    }
    _exigir(linha, "recorte", "classe", "posicao", "celulas", "nome", "modalidade")
    for campo, validos in (
        ("recorte", RECORTES_AP),
        ("classe", CLASSES_AP),
        ("modalidade", MODALIDADES_AP),
    ):
        if linha[campo] not in validos:
            raise ValueError(f"{campo} inválido: {linha[campo]!r}")
    return linha


# ---------------------------------------------------------------------------
# Floreser
# ---------------------------------------------------------------------------


def linha_floreser(p: dict) -> dict:
    """Registro do Floreser no padrão (aceita também os nomes do CSV legado)."""
    q = _minusculas(p)
    return _exigir(
        {
            "ano": _int(q.get("ano")),
            "cod_uf": _int(_primeiro(q, "cod_uf", "cd_geocuf")),
            "estado": _texto(q.get("estado")),
            "cod_mun": _int(_primeiro(q, "cod_mun", "cod_municipio")),
            "municipio": _texto(_primeiro(q, "municipio", "nome")),
            "idade": _int(q.get("idade")),
            "area_ha": _float(_primeiro(q, "area_ha", "area")),
        },
        "cod_uf",
        "estado",
        "cod_mun",
        "municipio",
        "idade",
        "area_ha",
    )


# ---------------------------------------------------------------------------
# SAD
# ---------------------------------------------------------------------------

# Coluna com o nome do território em cada camada (varia entre versões do export)
_TERRITORIO_SAD = {
    "assentamentos": ("territorio", "assentamen", "assentamento"),
    "terras_indigenas": ("territorio", "terra_indi", "ti", "terrai_nom"),
    "unidades_conservacao": ("territorio", "unid_conse", "uc"),
}


def linha_sad(p: dict, tipo: str, camada: str) -> dict:
    """Alerta do SAD no padrão. ``tipo`` e ``camada`` vêm do nome do arquivo.

    Aceita os nomes do export atual (ALERTA, MES, AREAKM2, NM_MUN/MUNICIPIO),
    dos ZIPs mensais antigos (Class_Name, Mes, Area, MUNICIPIOS) e do padrão.
    """
    q = _minusculas(p)
    ano, mes = _int(q.get("ano")), _int(q.get("mes"))
    if (ano is None or mes is None) and q.get("mes_ano"):  # ex.: "01_2026"
        mes, ano = (int(x) for x in str(q["mes_ano"]).split("_"))
    if mes is not None and not 1 <= mes <= 12:
        raise ValueError(f"mês inválido: {mes}")
    alerta = (_texto(_primeiro(q, "tipo", "alerta", "class_name")) or tipo).lower()
    if alerta != tipo:
        raise ValueError(f"alerta de {alerta!r} num arquivo de {tipo}")
    uc = camada == "unidades_conservacao"
    return _exigir(
        {
            "ano": ano,
            "mes": mes,
            "tipo": tipo,
            "camada": camada,
            "sensor": _texto(q.get("sensor")),
            "uf": _texto(_primeiro(q, "uf", "estado")),
            "municipio": _texto(_primeiro(q, "municipio", "nm_mun", "municipios")),
            "territorio": _texto(_primeiro(q, *_TERRITORIO_SAD.get(camada, ()))),
            "uso": _texto(q.get("uso")) if uc else None,
            "jurisdicao": _texto(q.get("jurisdicao")) if uc else None,
            # Conferida com a geometria na entrada (carga_sad.py)
            "area_km2": _float(_primeiro(q, "area_km2", "areakm2", "area")),
        },
        "ano",
        "mes",
    )
