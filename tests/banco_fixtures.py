"""Arquivos de envio de exemplo para os testes do banco."""

from __future__ import annotations

import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import box

from imazongeo_upload.banco.padrao import CAMADAS_SIMEX, CLASSES_AP, RECORTES_AP

# Nomes de arquivo como os recebidos hoje (um por camada)
ARQUIVOS_SIMEX = {
    "municipios": "simex_amz_{ano}_municipios.geojson",
    "imoveis_rurais": "simex_amz_{ano}_imoveisruraisprivados(1).geojson",
    "assentamentos": "simex_amz_{ano}_assentamentos(3).geojson",
    "terras_indigenas": "simex_amz_{ano}_TI(2).geojson",
    "unidades_conservacao": "simex_amz_{ano}_UC(2).geojson",
    "terras_nao_destinadas": "simex_amz_{ano}_terraspndest.geojson",
}


def zip_simex(
    destino: Path,
    ano: int = 2025,
    area_ha: float = 10.0,
    sem: str | None = None,
    gleba: str = "Gleba X",
    repetido: bool = False,
) -> Path:
    """ZIP com um GeoJSON por camada (esquema de 2024), sem atributo de camada.

    ``repetido`` duplica a feição da camada de assentamentos.
    """
    caminho = destino / f"simex_{ano}.zip"
    with zipfile.ZipFile(caminho, "w") as zf:
        for i, camada in enumerate(CAMADAS_SIMEX):
            if camada == sem:
                continue
            props = {
                "UF": ["PA"],
                "categoria": ["não autorizada" if i % 2 else "autorizada"],
                "Ano": [str(ano)],
                "NM_MUN": ["Paragominas"],
                "cd_mun": [1505502.0 if camada != "terras_indigenas" else None],
                "name": [gleba],
                "terrai_nom": ["Alto Rio Guamá"],
                "sub_class": ["SIGEF"],
                "area_ha": [area_ha + i],
            }
            gdf = gpd.GeoDataFrame(
                props, geometry=[box(-47.5, -3.1 + i / 100, -47.4, -3.0)], crs=4674
            )
            if repetido and camada == "assentamentos":
                gdf = pd.concat([gdf, gdf], ignore_index=True)
            arquivo = destino / ARQUIVOS_SIMEX[camada].format(ano=ano)
            gdf.to_file(arquivo, driver="GeoJSON")
            zf.write(arquivo, arquivo.name)
            arquivo.unlink()
    return caminho


def geojson_ap(
    destino: Path,
    ano: int = 2025,
    trimestre: int = 3,
    area_nome: str = "TI Apyterewa",
    geom=None,
    celulas: int = 40,
    legenda: str = "julho a setembro",
) -> Path:
    """GeoJSON de um trimestre com os 8 rankings (esquema de 2020-2023)."""
    linhas = []
    for recorte in RECORTES_AP:
        for classe in CLASSES_AP:
            linhas.append(
                {
                    "dado": recorte,
                    "class": classe,
                    "nome": area_nome,
                    "estado": "PA",
                    "jurisdicao": "Federal",
                    "modalidade": "ti",
                    "categoria": "Terra Indígena",
                    "uso": "Terra Indigena",
                    "rank": 1,
                    "celulas": celulas,
                    "ano": ano,
                    "periodo": trimestre,
                    "legend": legenda,
                }
            )
    gdf = gpd.GeoDataFrame(
        linhas, geometry=[geom or box(-52, -5, -51, -4)] * len(linhas), crs=4326
    )
    caminho = destino / f"ap_{ano}_t{trimestre}.geojson"
    gdf.to_file(caminho, driver="GeoJSON")
    return caminho


def csv_floreser(destino: Path, ano: int = 2024, area: float = 10.5) -> Path:
    caminho = destino / f"floreser_{ano}.csv"
    caminho.write_text(
        "index,ano,area,CD_GEOCUF,cod_municipio,IDADE,estado,nome\n"
        f"38_1,{ano},{area},11,1100205,1,Rondônia,Porto Velho\n"
        f"38_2,{ano},0.1,13,1100205,1,Amazonas,Porto Velho\n",
        encoding="utf-8",
    )
    return caminho


def geojson_sad(
    destino: Path,
    alertas: list[dict],
    tipo: str = "desmatamento",
    camada: str = "unidadesConservacao",
    inicio: tuple[int, int] = (1, 2025),
    fim: tuple[int, int] = (3, 2025),
) -> Path:
    """Arquivo acumulado do SAD (export atual). Cada alerta: ano, mes e extras.

    As geometrias são quadrados de 0,01° (≈ 1,23 km²) em posições distintas.
    """
    linhas = []
    for i, a in enumerate(alertas):
        linhas.append(
            {
                "ALERTA": tipo,
                "MES": str(a["mes"]),
                "ANO": str(a["ano"]),
                "SENSOR": "Sentinel-2",
                "ESTADO": a.get("uf", "PA"),
                "AREAKM2": a.get("area", 1.2308),
                "MUNICIPIO": a.get("municipio", "Altamira"),
                "UC": a.get("territorio", "APA Triunfo do Xingu"),
                "USO": a.get("uso", "Uso Sustentável"),
                "JURISDICAO": "Estadual",
                "geometry": a.get("geom")
                or box(-52 + i / 50, -5, -51.99 + i / 50, -4.99),
            }
        )
    nome = (
        f"alertas_sad_{tipo}_{inicio[0]:02d}_{inicio[1]}_{fim[0]:02d}_{fim[1]}"
        f"_{camada}.geojson"
    )
    caminho = destino / nome
    gpd.GeoDataFrame(linhas, crs=4326).to_file(caminho, driver="GeoJSON")
    return caminho
