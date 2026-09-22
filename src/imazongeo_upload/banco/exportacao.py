"""Geração dos arquivos de download a partir do banco (padrão em padrao.py).

Os três formatos têm os mesmos campos, na mesma ordem:

- GeoJSON (WGS 84, coordenadas com precisão total: arredondar, como no modo
  RFC 7946 do GDAL, transforma polígonos muito finos em linhas);
- CSV UTF-8 com BOM (abre com acentos corretos no Excel), sem geometria;
- Shapefile compactado em ZIP (.shp, .shx, .dbf, .prj, .cpg em UTF-8).
"""

from __future__ import annotations

import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pyogrio

from .padrao import FORMATOS, Padrao, Periodo

# "Created N records" a cada arquivo gravado
logging.getLogger("pyogrio").setLevel(logging.WARNING)


@dataclass(frozen=True)
class ArquivoGerado:
    formato: str
    caminho: Path
    chave_s3: str


def escrever_arquivos(
    df: pd.DataFrame, p: Padrao, periodo: Periodo, destino: Path
) -> list[ArquivoGerado]:
    """Grava os formatos do padrão em ``destino`` a partir dos registros."""
    if df.empty:
        raise ValueError(f"Não há registros de {p.slug} {periodo} para exportar.")
    if list(df.columns[: len(p.nomes)]) != p.nomes:
        raise ValueError(f"Colunas fora do padrão: {list(df.columns)}")
    destino.mkdir(parents=True, exist_ok=True)
    base = f"{p.raiz_s3}_{periodo.sufixo}"
    gerados = []
    for formato in p.formatos:
        caminho = destino / f"{base}.{FORMATOS[formato][1]}"
        if formato == "geojson":
            pyogrio.write_dataframe(df, caminho, driver="GeoJSON")
        elif formato == "csv":
            tabela = pd.DataFrame(df[p.nomes])
            tabela.to_csv(caminho, index=False, encoding="utf-8-sig")
        else:
            _escrever_shapefile(df, destino / "_shp" / base, caminho)
        gerados.append(ArquivoGerado(formato, caminho, p.chave_s3(periodo, formato)))
        logging.info("Gerado %s (%.1f MB)", caminho.name, caminho.stat().st_size / 1e6)
    return gerados


def _escrever_shapefile(df: pd.DataFrame, base: Path, zip_saida: Path) -> None:
    base.parent.mkdir(parents=True, exist_ok=True)
    pyogrio.write_dataframe(
        df, base.with_suffix(".shp"), driver="ESRI Shapefile", encoding="UTF-8"
    )
    partes = sorted(base.parent.glob(base.name + ".*"))
    with zipfile.ZipFile(zip_saida, "w", zipfile.ZIP_DEFLATED) as zf:
        for parte in partes:
            zf.write(parte, parte.name)
            parte.unlink()
