"""Concatenação dos arquivos locais com as versões já publicadas no S3.

Em todos os formatos, os registros do S3 cujo período (MES, ANO) também
existe no arquivo local são descartados antes de juntar, evitando
duplicatas quando um período é reenviado.
"""

from __future__ import annotations

import csv
import json
import logging
import zipfile
from pathlib import Path
from typing import Any

from .s3 import baixar_arquivo_s3


def _periodos_do_local_csv(local_path: Path) -> set:
    """Retorna conjunto de (MES, ANO) presentes no CSV local."""
    periodos: set = set()
    with open(local_path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            try:
                periodos.add((int(row["MES"]), int(row["ANO"])))
            except (KeyError, ValueError):
                pass
    return periodos


def _periodos_do_local_geojson(local_path: Path) -> set:
    """Retorna conjunto de (MES, ANO) presentes no GeoJSON local."""
    with open(local_path, encoding="utf-8") as f:
        data = json.load(f)
    periodos: set = set()
    for feat in data.get("features", []):
        props = feat.get("properties", {})
        try:
            periodos.add((int(props["MES"]), int(props["ANO"])))
        except (KeyError, ValueError):
            pass
    return periodos


def concatenar_csv(s3_path: Path, local_path: Path, output_path: Path) -> None:
    """Concatena o CSV do S3 (s3_path) com o CSV local (local_path).

    Remove do S3 as linhas cujo (MES, ANO) já existe no arquivo local.
    O cabeçalho não é duplicado.
    """
    periodos_novos = _periodos_do_local_csv(local_path)

    with open(s3_path, encoding="utf-8", newline="") as f_s3:
        reader = csv.DictReader(f_s3)
        fieldnames = reader.fieldnames or []
        total_s3 = 0
        linhas_s3 = []
        for row in reader:
            total_s3 += 1
            periodo = (int(row.get("MES", 0)), int(row.get("ANO", 0)))
            if periodo not in periodos_novos:
                linhas_s3.append(row)

    removidas = total_s3 - len(linhas_s3)
    if removidas:
        logging.info(
            "CSV: removidas %d linhas do S3 com período duplicado %s "
            "antes de concatenar.",
            removidas,
            sorted(periodos_novos),
        )

    with open(local_path, encoding="utf-8", newline="") as f_local:
        reader_local = csv.DictReader(f_local)
        linhas_local = list(reader_local)
        if not fieldnames:
            fieldnames = reader_local.fieldnames or []

    with open(output_path, "w", encoding="utf-8", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(linhas_s3)
        writer.writerows(linhas_local)

    logging.info(
        "CSV concatenado: %d linhas S3 + %d linhas local -> %s",
        len(linhas_s3),
        len(linhas_local),
        output_path.name,
    )


def concatenar_geojson(s3_path: Path, local_path: Path, output_path: Path) -> None:
    """Junta os arrays 'features' de dois GeoJSON.

    Remove do S3 as feições cujo (MES, ANO) já existe no arquivo local.
    """
    with open(s3_path, encoding="utf-8") as f:
        base = json.load(f)
    with open(local_path, encoding="utf-8") as f:
        novo = json.load(f)

    periodos_novos = _periodos_do_local_geojson(local_path)

    base_features_originais = base.get("features", [])
    base_features_filtradas = [
        feat
        for feat in base_features_originais
        if (
            int(feat.get("properties", {}).get("MES", 0)),
            int(feat.get("properties", {}).get("ANO", 0)),
        )
        not in periodos_novos
    ]
    novo_features = novo.get("features", [])

    removidas = len(base_features_originais) - len(base_features_filtradas)
    if removidas:
        logging.info(
            "GeoJSON: removidas %d features do S3 com período duplicado %s "
            "antes de concatenar.",
            removidas,
            sorted(periodos_novos),
        )

    base["features"] = base_features_filtradas + novo_features

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(base, f, ensure_ascii=False)

    logging.info(
        "GeoJSON concatenado: %d features S3 + %d features local -> %s",
        len(base_features_filtradas),
        len(novo_features),
        output_path.name,
    )


def concatenar_shapefile(s3_path: Path, local_path: Path, output_path: Path) -> None:
    """Junta dois shapefiles empacotados em .zip e recompacta o resultado.

    Remove do S3 as feições cujo (MES, ANO) já existe no shapefile local.
    """
    try:
        import geopandas as gpd
        import pandas as pd
    except ImportError as exc:
        raise RuntimeError(
            "geopandas e pandas são necessários para concatenar shapefiles.\n"
            "Instale com: pip install geopandas pandas"
        ) from exc

    tmp = output_path.parent / "_shp_tmp"
    tmp.mkdir(exist_ok=True)

    dir_s3 = tmp / "s3"
    dir_local = tmp / "local"
    dir_merged = tmp / "merged"
    for d in (dir_s3, dir_local, dir_merged):
        d.mkdir(exist_ok=True)

    with zipfile.ZipFile(s3_path) as z:
        z.extractall(dir_s3)
    with zipfile.ZipFile(local_path) as z:
        z.extractall(dir_local)

    shp_s3 = next(dir_s3.rglob("*.shp"), None)
    shp_local = next(dir_local.rglob("*.shp"), None)
    if shp_s3 is None:
        raise FileNotFoundError(f"Nenhum .shp encontrado dentro de {s3_path.name}")
    if shp_local is None:
        raise FileNotFoundError(f"Nenhum .shp encontrado dentro de {local_path.name}")

    gdf_s3 = gpd.read_file(shp_s3)
    gdf_local = gpd.read_file(shp_local)

    # Deduplica: remove do S3 os períodos que já existem no local
    if "MES" in gdf_local.columns and "ANO" in gdf_local.columns:
        periodos_novos = set(
            zip(gdf_local["MES"].astype(int), gdf_local["ANO"].astype(int), strict=True)
        )
        if "MES" in gdf_s3.columns and "ANO" in gdf_s3.columns:
            periodos_s3 = zip(
                gdf_s3["MES"].astype(int), gdf_s3["ANO"].astype(int), strict=True
            )
            mascara = ~pd.Series(periodos_s3).isin(periodos_novos)
            removidas = (~mascara).sum()
            gdf_s3 = gdf_s3[mascara.values]
            if removidas:
                logging.info(
                    "Shapefile: removidas %d feições do S3 com período duplicado "
                    "%s antes de concatenar.",
                    removidas,
                    sorted(periodos_novos),
                )

    merged = pd.concat([gdf_s3, gdf_local], ignore_index=True)

    merged_shp = dir_merged / f"{local_path.stem}.shp"
    merged.to_file(merged_shp)

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zout:
        for part in dir_merged.iterdir():
            zout.write(part, part.name)

    logging.info(
        "Shapefile concatenado: %d feições S3 + %d feições local -> %s",
        len(gdf_s3),
        len(gdf_local),
        output_path.name,
    )


_CONCAT_FN = {
    ".csv": concatenar_csv,
    ".geojson": concatenar_geojson,
    ".zip": concatenar_shapefile,
}


def concatenar_com_s3(
    arquivos: list[tuple[Path, str]],
    bucket: str,
    s3_client: Any,
    tmp_dir: Path,
) -> list[tuple[Path, str]]:
    """Junta cada arquivo local com a versão equivalente do S3.

    Para cada (local_path, s3_prefix), tenta baixar o arquivo do S3. Se
    existir, concatena local + S3 e devolve o caminho do resultado; se não
    existir (primeiro upload), devolve o caminho local inalterado.
    """
    resultado: list[tuple[Path, str]] = []

    for local_path, s3_prefix in arquivos:
        key = s3_prefix + local_path.name
        s3_local = tmp_dir / f"s3_{local_path.name}"
        merged = tmp_dir / local_path.name

        if not baixar_arquivo_s3(s3_client, bucket, key, s3_local):
            resultado.append((local_path, s3_prefix))
            continue

        ext = local_path.suffix.lower()
        fn = _CONCAT_FN.get(ext)
        if fn is None:
            logging.warning(
                "Formato sem suporte a concatenação: %s — fazendo upload simples.",
                ext,
            )
            resultado.append((local_path, s3_prefix))
        else:
            fn(s3_local, local_path, merged)
            resultado.append((merged, s3_prefix))

    return resultado
