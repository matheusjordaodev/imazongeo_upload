#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Upload (ou simulação) de datasets SAD / Floreser / Ameaça & Pressão / SIMEX para S3.

Todos os arquivos seguem o padrão de nome:

    nome_ano_mes.extensão  ->  ex.: sad_2025_11.zip

e são enviados em 3 formatos (shapefile, csv, geojson) para subpastas no S3.

Credenciais AWS:
    Lidas do arquivo .env (via python-dotenv), com as variáveis:
        ACCESS_KEY=
        PRIVATE_KEY=
        AWS_REGION=   (opcional, default us-east-1 se não definido)

Uso básico:

  # SAD: simulação (dry-run, default) a partir do ZIP recebido
  python main.py sad --bucket imazongeo3-web --zip shapefile.zip

  # SAD: upload real + objetos públicos (ACL public-read), com o download
  # dividido em partes e arquivos mensais de todos os meses do ZIP
  python main.py sad --bucket imazongeo3-web --zip parte1.zip --zip parte2.zip --todos-meses --no-dry-run --public

  # Demais datasets (arquivos em dados/{dataset}/...)
  python main.py simex --bucket imazongeo3-web --year 2025 --no-dry-run
"""

import argparse
import json
import logging
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple

from dotenv import load_dotenv

# Carrega variáveis do .env na raiz do projeto
load_dotenv()

try:
    import boto3  # necessário para upload real
except ImportError:
    boto3 = None


FORMATS = ["shapefile", "csv", "geojson"]


def _dashboard_s3_prefix(s3_root: str) -> str:
    """Padrão: dashboard/{dataset}/geojson/"""
    return f"dashboard/{s3_root}/geojson/"


@dataclass
class DatasetConfig:
    slug: str         # 'sad', 'floreser', 'ameaca_pressao', 'simex'
    descricao: str    # descrição amigável
    frequencia: str   # 'monthly', 'quarterly', 'annual'
    local_subdir: str # subpasta dentro de base_dir
    s3_root: str      # raiz dentro do bucket (ex: 'sad', 'floreser', 'ameaca_e_pressao', 'simex')
    dashboard_root: str = ""  # pasta em dashboard/ lida pelo dashboard, se diferente de s3_root


DATASETS = {
    "sad": DatasetConfig(
        slug="sad",
        descricao="SAD Mensal",
        frequencia="monthly",
        local_subdir="sad",
        s3_root="sad",
    ),
    "floreser": DatasetConfig(
        slug="floreser",
        descricao="Floreser Anual",
        frequencia="annual",
        local_subdir="floreser",
        s3_root="floreser",
    ),
    "ameaca_pressao": DatasetConfig(
        slug="ameaca_pressao",
        descricao="Ameaça & Pressão Trimestral",
        frequencia="quarterly",
        local_subdir="ameaca_pressao",
        s3_root="ameaca_e_pressao",
        dashboard_root="ap",
    ),
    "simex": DatasetConfig(
        slug="simex",
        descricao="SIMEX Anual",
        frequencia="annual",
        local_subdir="simex",
        s3_root="simex",
    ),
}


# ----------------------------------------------------------------------
# Funções auxiliares
# ----------------------------------------------------------------------

def setup_logging(verboso: bool = False) -> None:
    nivel = logging.DEBUG if verboso else logging.INFO
    logging.basicConfig(
        level=nivel,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def create_s3_client():
    """
    Cria o client S3 usando credenciais do .env:

        ACCESS_KEY=
        PRIVATE_KEY=
        AWS_REGION=  (opcional, default us-east-1)
    """
    if boto3 is None:
        raise RuntimeError(
            "boto3 não está instalado. Instale com 'pip install boto3'."
        )

    access_key = os.getenv("ACCESS_KEY")
    secret_key = os.getenv("PRIVATE_KEY")
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"

    if not access_key or not secret_key:
        raise RuntimeError(
            "Variáveis ACCESS_KEY e PRIVATE_KEY não encontradas no ambiente/.env.\n"
            "Adicione ao seu .env, por exemplo:\n"
            "  ACCESS_KEY=seu_access_key_id\n"
            "  PRIVATE_KEY=seu_secret_access_key\n"
            "  AWS_REGION=us-east-1"
        )

    return boto3.client(
        "s3",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
    )


# ----------------------------------------------------------------------
# Download e concatenação com arquivo existente no S3
# ----------------------------------------------------------------------

def baixar_arquivo_s3(s3_client, bucket: str, key: str, dest_path: Path) -> bool:
    """
    Baixa um arquivo do S3 para dest_path.
    Retorna True se o arquivo existia e foi baixado, False se não existia (404/NoSuchKey).
    Outros erros (permissão, rede) propagam normalmente.
    """
    try:
        s3_client.download_file(bucket, key, str(dest_path))
        logging.info("Baixado do S3: s3://%s/%s -> %s", bucket, key, dest_path)
        return True
    except Exception as exc:
        # botocore.exceptions.ClientError: código 404 ou NoSuchKey
        code = getattr(getattr(exc, "response", {}), "get", lambda *a: None)(
            "Error", {}
        ).get("Code", "")
        if not code:
            try:
                code = exc.response["Error"]["Code"]  # type: ignore[attr-defined]
            except Exception:
                code = ""
        if code in ("404", "NoSuchKey"):
            logging.info("Arquivo não encontrado no S3 (será primeiro upload): s3://%s/%s", bucket, key)
            return False
        raise


def _periodos_do_local_csv(local_path: Path) -> set:
    """Retorna conjunto de (MES, ANO) presentes no CSV local."""
    import csv
    periodos: set = set()
    with open(local_path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
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
    """
    Concatena o CSV existente no S3 (s3_path) com o CSV local (local_path).
    Remove do S3 as linhas cujo (MES, ANO) já existe no arquivo local,
    evitando duplicatas de período. Header não é duplicado.
    """
    import csv

    periodos_novos = _periodos_do_local_csv(local_path)

    with open(s3_path, encoding="utf-8", newline="") as f_s3:
        reader = csv.DictReader(f_s3)
        fieldnames = reader.fieldnames or []
        linhas_s3 = [
            row for row in reader
            if (int(row.get("MES", 0)), int(row.get("ANO", 0))) not in periodos_novos
        ]

    removidas = 0
    with open(s3_path, encoding="utf-8", newline="") as f_s3:
        total_s3 = sum(1 for _ in csv.DictReader(f_s3))
    removidas = total_s3 - len(linhas_s3)
    if removidas:
        logging.info(
            "CSV: removidas %d linhas do S3 com período duplicado %s antes de concatenar.",
            removidas, sorted(periodos_novos),
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
        len(linhas_s3), len(linhas_local), output_path.name,
    )


def concatenar_geojson(s3_path: Path, local_path: Path, output_path: Path) -> None:
    """
    Merge de dois GeoJSON: une os arrays 'features'.
    Remove do S3 as features cujo (MES, ANO) já existe no arquivo local,
    evitando duplicatas de período.
    """
    with open(s3_path, encoding="utf-8") as f:
        base = json.load(f)
    with open(local_path, encoding="utf-8") as f:
        novo = json.load(f)

    periodos_novos = _periodos_do_local_geojson(local_path)

    base_features_originais = base.get("features", [])
    base_features_filtradas = [
        feat for feat in base_features_originais
        if (
            int(feat.get("properties", {}).get("MES", 0)),
            int(feat.get("properties", {}).get("ANO", 0)),
        ) not in periodos_novos
    ]
    novo_features = novo.get("features", [])

    removidas = len(base_features_originais) - len(base_features_filtradas)
    if removidas:
        logging.info(
            "GeoJSON: removidas %d features do S3 com período duplicado %s antes de concatenar.",
            removidas, sorted(periodos_novos),
        )

    base["features"] = base_features_filtradas + novo_features

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(base, f, ensure_ascii=False)

    logging.info(
        "GeoJSON concatenado: %d features S3 + %d features local -> %s",
        len(base_features_filtradas), len(novo_features), output_path.name,
    )


def concatenar_shapefile(s3_path: Path, local_path: Path, output_path: Path) -> None:
    """
    Merge de dois shapefiles empacotados em .zip.
    Remove do S3 as linhas cujo (MES, ANO) já existe no shapefile local,
    evitando duplicatas de período. Recompacta o resultado em .zip.
    """
    try:
        import geopandas as gpd
        import pandas as pd
    except ImportError:
        raise RuntimeError(
            "geopandas e pandas são necessários para concatenar shapefiles.\n"
            "Instale com: pip install geopandas pandas"
        )

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
            zip(gdf_local["MES"].astype(int), gdf_local["ANO"].astype(int))
        )
        if "MES" in gdf_s3.columns and "ANO" in gdf_s3.columns:
            mascara = ~(
                pd.Series(zip(gdf_s3["MES"].astype(int), gdf_s3["ANO"].astype(int)))
                .isin(periodos_novos)
            )
            removidas = (~mascara).sum()
            gdf_s3 = gdf_s3[mascara.values]
            if removidas:
                logging.info(
                    "Shapefile: removidas %d feições do S3 com período duplicado %s antes de concatenar.",
                    removidas, sorted(periodos_novos),
                )

    merged = pd.concat([gdf_s3, gdf_local], ignore_index=True)

    stem = local_path.stem
    merged_shp = dir_merged / f"{stem}.shp"
    merged.to_file(merged_shp)

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zout:
        for part in dir_merged.iterdir():
            zout.write(part, part.name)

    logging.info(
        "Shapefile concatenado: %d feições S3 + %d feições local -> %s",
        len(gdf_s3), len(gdf_local), output_path.name,
    )

    logging.info(
        "Shapefile concatenado: %d + %d feições -> %s",
        len(gdf_s3), len(gdf_local), output_path.name,
    )


_CONCAT_FN = {
    ".csv":     concatenar_csv,
    ".geojson": concatenar_geojson,
    ".zip":     concatenar_shapefile,
}


def concatenar_com_s3(
    arquivos: List[Tuple[Path, str]],
    bucket: str,
    s3_client,
    tmp_dir: Path,
) -> List[Tuple[Path, str]]:
    """
    Para cada (local_path, s3_prefix), tenta baixar o arquivo equivalente do S3.
    Se existir, concatena local + S3 e retorna o path do arquivo merged.
    Se não existir (primeiro upload), retorna o path local inalterado.
    """
    resultado: List[Tuple[Path, str]] = []

    for local_path, s3_prefix in arquivos:
        key = s3_prefix + local_path.name
        s3_local = tmp_dir / f"s3_{local_path.name}"
        merged = tmp_dir / local_path.name

        baixou = baixar_arquivo_s3(s3_client, bucket, key, s3_local)
        if baixou:
            ext = local_path.suffix.lower()
            fn = _CONCAT_FN.get(ext)
            if fn is None:
                logging.warning("Formato sem suporte a concatenação: %s — fazendo upload simples.", ext)
                resultado.append((local_path, s3_prefix))
            else:
                fn(s3_local, local_path, merged)
                resultado.append((merged, s3_prefix))
        else:
            resultado.append((local_path, s3_prefix))

    return resultado


def descobrir_periodo(cfg: DatasetConfig,
                      year: Optional[int],
                      month: Optional[int],
                      quarter: Optional[int]) -> dict:
    """
    Descobre ano/mês/trimestre com base no que foi passado + data atual.
    """
    hoje = date.today()
    ano = year or hoje.year

    if cfg.frequencia == "monthly":
        mes = month or hoje.month
        return {"year": ano, "month": mes}
    elif cfg.frequencia == "quarterly":
        if quarter:
            q = quarter
        else:
            q = (hoje.month - 1) // 3 + 1
        return {"year": ano, "quarter": q}
    else:  # annual
        return {"year": ano}


_FORMAT_EXT = {
    "csv":       ".csv",
    "shapefile": ".zip",
    "geojson":   ".geojson",
}


def montar_nome_arquivo(cfg: DatasetConfig, periodo: dict, tipo: str = "") -> str:
    """
    Monta o nome do arquivo de acordo com o dataset e o período.

    - SAD:             alertas_sad_MM_YYYY.{ext}
    - SIMEX (anual):   simex_unificado_YYYY.{ext}
    - Floreser (anual):floreser_YYYY_12.zip
    - A&P (trimestral):ameaca_e_pressao_{Q}_trimestre_YYYY.{ext}
    """
    year = periodo["year"]
    ext = _FORMAT_EXT.get(tipo, ".zip")

    if cfg.slug == "sad":
        month = periodo["month"]
        return f"alertas_sad_{month:02d}_{year}{ext}"

    elif cfg.slug == "simex":
        return f"simex_unificado_{year}{ext}"

    elif cfg.slug == "floreser":
        return f"floreser_{year}{ext}"

    elif cfg.slug == "ameaca_pressao":
        q = periodo["quarter"]
        return f"ameaca_e_pressao_{q}_trimestre_{year}{ext}"

    else:
        raise ValueError(f"Dataset não suportado: {cfg.slug}")


def montar_diretorio_base_local(base_dir: Path,
                                cfg: DatasetConfig,
                                periodo: dict) -> Path:
    """
    Retorna o diretório base local (sem o /<tipo>) onde os arquivos do dataset devem estar.
    """
    root = base_dir / cfg.local_subdir

    if cfg.frequencia == "monthly":
        return root / f"{periodo['year']}" / f"{periodo['month']:02d}"
    elif cfg.frequencia == "quarterly":
        return root / f"{periodo['year']}" / f"T{periodo['quarter']}"
    else:  # annual
        return root / f"{periodo['year']}"


def coletar_arquivos_multi_tipo(
    base_dir: Path,
    cfg: DatasetConfig,
    periodo: dict,
    formats: Optional[List[str]] = None,
    op: str = "download",
) -> List[Tuple[Path, str]]:
    """
    Para um dataset, coleta os arquivos ZIP esperados para cada tipo
    (shapefile, csv, geojson), caso existam, e retorna:

        [(caminho_arquivo, s3_prefix), ...]

    op="download"  → s3_prefix = '{s3_root}/{fmt}/'
    op="dashboard" → s3_prefix = _prefixo_dashboard(cfg, fmt)

    formats: subconjunto de FORMATS a processar; None = todos.
    """
    base_local = montar_diretorio_base_local(base_dir, cfg, periodo)

    arquivos: List[Tuple[Path, str]] = []

    if not base_local.exists():
        logging.warning("Pasta base local não encontrada: %s", base_local)

    for tipo in (formats or FORMATS):
        nome_arquivo = montar_nome_arquivo(cfg, periodo, tipo)
        dir_tipo = base_local / tipo
        caminho_arquivo = dir_tipo / nome_arquivo

        if caminho_arquivo.exists():
            if op == "dashboard":
                s3_prefix = _prefixo_dashboard(cfg, tipo)
            else:
                s3_prefix = f"{cfg.s3_root}/{tipo}/"
            arquivos.append((caminho_arquivo, s3_prefix))
            logging.info(
                "Arquivo %s (%s) encontrado: %s",
                cfg.slug,
                tipo,
                caminho_arquivo,
            )
        else:
            logging.warning(
                "Arquivo %s tipo '%s' não encontrado (esperado: %s)",
                cfg.slug,
                tipo,
                caminho_arquivo,
            )

    return arquivos


def enviar_arquivos_para_s3_multi(
    arquivos: List[Tuple[Path, str]],
    bucket: str,
    dry_run: bool = True,
    public: bool = False,
) -> None:
    """
    Envia (ou simula enviar) arquivos para S3,
    permitindo um prefixo diferente por arquivo.

    arquivos: lista de tuplas (Path, prefixo_s3)
    """
    if not arquivos:
        logging.info("Nenhum arquivo para enviar (multi).")
        return

    if dry_run:
        logging.info("### MODO SIMULAÇÃO (dry-run): nada será enviado de fato ###")
        if public:
            logging.info("Flag --public ativa (simulação de objetos públicos).")
        s3_client = None
    else:
        s3_client = create_s3_client()

    for arq, prefixo_s3 in arquivos:
        key = prefixo_s3 + arq.name
        if dry_run:
            logging.info("[DRY RUN] Subiria %s para s3://%s/%s", arq, bucket, key)
            if public:
                logging.info("[DRY RUN] Definiria ACL public-read em s3://%s/%s", bucket, key)
        else:
            logging.info("Enviando %s para s3://%s/%s ...", arq, bucket, key)
            s3_client.upload_file(str(arq), bucket, key)
            logging.info("OK: %s -> s3://%s/%s", arq.name, bucket, key)

            if public:
                logging.info("Definindo ACL public-read para s3://%s/%s ...", bucket, key)
                s3_client.put_object_acl(
                    Bucket=bucket,
                    Key=key,
                    ACL="public-read",
                )
                logging.info("ACL public-read aplicada em s3://%s/%s", bucket, key)


# Prefixo base para uploads de dashboard
_S3_DASHBOARD_BASE = "dashboard"


def _s3_dashboard_prefix(dataset_s3_root: str, fmt: str) -> str:
    """
    Monta o prefixo S3 de dashboard dinamicamente:
        {_S3_DASHBOARD_BASE}/{dataset}/{fmt}/

    Exemplo (teste):   teste_validacao/dashboard/sad/geojson/
    Exemplo (produção, base=""): sad/geojson/
    """
    base = _S3_DASHBOARD_BASE.rstrip("/")
    if base:
        return f"{base}/{dataset_s3_root}/{fmt}/"
    return f"{dataset_s3_root}/{fmt}/"


def _prefixo_dashboard(cfg: DatasetConfig, fmt: str) -> str:
    """Prefixo de dashboard de um dataset (A&P usa a pasta 'ap', lida pelo dashboard)."""
    return _s3_dashboard_prefix(cfg.dashboard_root or cfg.s3_root, fmt)


# ----------------------------------------------------------------------
# SAD: ZIP recebido com os alertas acumulados (um shapefile por tipo + camada)
# ----------------------------------------------------------------------

# Nome da camada no arquivo recebido (em minúsculas) → nome padronizado
# usado nos arquivos de saída. O export do SAD não é consistente entre os
# tipos ("terrasIndigenas" x "terraIndigena", "unidadesConservacao" x
# "unidadeConservacao"), por isso as variações.
_SAD_CAMADAS = {
    "amazonialegal":       "amazoniaLegal",
    "municipios":          "municipios",
    "assentamentos":       "assentamentos",
    "terrasindigenas":     "terraIndigena",
    "terraindigena":       "terraIndigena",
    "unidadesconservacao": "unidadeConservacao",
    "unidadeconservacao":  "unidadeConservacao",
}

# alertas_sad_{tipo}_{MM}_{AAAA}[_{MM}_{AAAA}]_{camada}[__polygons]
_SAD_NOME_RE = re.compile(
    r"^alertas_sad_(?P<tipo>desmatamento|degradacao)_"
    r"(?P<periodos>(?:\d{1,2}_\d{4}_)+)"
    r"(?P<camada>[A-Za-z]+)(?:__polygons)?$",
    re.IGNORECASE,
)

# Nome dos CSVs em dashboard/sad/csv/ por tipo + camada, no padrão já
# consumido pelo dashboard (C:\dashboards\sad\index.html) — inclui a
# inconsistência real do projeto ("assentamento" sem "s" na degradação).
_SAD_CSV_DASHBOARD = {
    "desmatamento": {
        "amazoniaLegal":      "amazoniaLegal",
        "municipios":         "municipios",
        "assentamentos":      "assentamentos",
        "terraIndigena":      "terra_indigena",
        "unidadeConservacao": "ucs",
    },
    "degradacao": {
        "amazoniaLegal":      "amazoniaLegal",
        "municipios":         "municipios",
        "assentamentos":      "assentamento",
        "terraIndigena":      "terra_indigena",
        "unidadeConservacao": "ucs",
    },
}

# Colunas (e ordem) dos CSVs do dashboard, com os nomes que o dashboard usa
# para identificar cada território (MUNICIPIO, TERRA_INDI, UNID_CONSE…).
_SAD_COLUNAS_BASE = ["ALERTA", "MES", "ANO", "SENSOR", "ESTADO", "AREAKM2"]
_SAD_COLUNAS_DASHBOARD = {
    "amazoniaLegal":      [],
    "municipios":         ["MUNICIPIO"],
    "assentamentos":      ["MUNICIPIO", "ASSENTAMEN"],
    "terraIndigena":      ["MUNICIPIO", "TERRA_INDI"],
    "unidadeConservacao": ["MUNICIPIO", "UNID_CONSE", "USO", "JURISDICAO"],
}

# A coluna de município muda de nome entre as camadas do arquivo recebido
# (NM_MUN, MUNICIPIOS, MUNICIPIO); em todas as saídas ela vira MUNICIPIO.
_SAD_RENOMEAR = {"NM_MUN": "MUNICIPIO", "MUNICIPIOS": "MUNICIPIO"}
_SAD_RENOMEAR_DASHBOARD = {"TI": "TERRA_INDI", "UC": "UNID_CONSE"}

# Arquivos extraídos do ZIP (o resto, como .qix e .qmd, é ignorado)
_SAD_EXTENSOES = {".shp", ".shx", ".dbf", ".prj", ".cpg", ".geojson"}


@dataclass
class CamadaSAD:
    tipo: str                  # 'desmatamento' | 'degradacao'
    camada: str                # nome padronizado (valores de _SAD_CAMADAS)
    path: Path                 # .shp ou .geojson extraído do ZIP
    reprojetar: bool = False   # True se não estiver em EPSG:4326 (exigido no GeoJSON)


def _importar_libs_geo():
    """Importa geopandas, pandas e pyogrio, com mensagem clara se faltarem."""
    try:
        import geopandas as gpd
        import pandas as pd
        import pyogrio
    except ImportError:
        raise RuntimeError(
            "geopandas, pandas e pyogrio são necessários para processar o SAD.\n"
            "Instale com: pip install geopandas pandas pyogrio"
        )
    # O pyogrio registra "Created N records" (INFO) a cada arquivo gravado
    logging.getLogger("pyogrio").setLevel(logging.WARNING)
    return gpd, pd, pyogrio


def _identificar_camada_sad(nome_arquivo: str) -> Optional[Tuple[str, str, List[Tuple[int, int]]]]:
    """
    Reconhece tipo, camada e períodos (ano, mês) pelo nome do arquivo:

    alertas_sad_desmatamento_01_2008_07_2026_terrasIndigenas.shp
        → ("desmatamento", "terraIndigena", [(2008, 1), (2026, 7)])

    Retorna None se o nome não seguir o padrão do SAD.
    """
    m = _SAD_NOME_RE.match(Path(nome_arquivo).stem)
    if not m:
        return None
    camada = _SAD_CAMADAS.get(m.group("camada").lower())
    if camada is None:
        return None
    nums = [int(n) for n in m.group("periodos").strip("_").split("_")]
    periodos = [(nums[i + 1], nums[i]) for i in range(0, len(nums), 2)]
    return m.group("tipo").lower(), camada, periodos


def inspecionar_zips_sad(zips: List[Path]) -> dict:
    """
    Resume o conteúdo dos ZIPs do SAD lendo só a lista de arquivos (sem
    extrair): camadas encontradas e período coberto pelos nomes.

    Retorna {"camadas": [(tipo, camada), ...],
             "inicio": (ano, mes) | None, "fim": (ano, mes) | None}.
    """
    camadas = set()
    periodos: List[Tuple[int, int]] = []
    for zip_path in zips:
        with zipfile.ZipFile(zip_path) as zf:
            for nome in zf.namelist():
                if Path(nome).suffix.lower() not in (".shp", ".geojson"):
                    continue
                info = _identificar_camada_sad(nome)
                if info:
                    camadas.add((info[0], info[1]))
                    periodos.extend(info[2])
    return {
        "camadas": sorted(camadas),
        "inicio": min(periodos) if periodos else None,
        "fim": max(periodos) if periodos else None,
    }


def _extrair_zips_sad(zips: List[Path], destino: Path) -> List[CamadaSAD]:
    """
    Extrai as partes relevantes dos ZIPs para uma única pasta — o download
    do Google Drive divide um mesmo shapefile entre vários ZIPs (ex.: o .shp
    num e o .dbf em outro) — e retorna as camadas reconhecidas.
    """
    for zip_path in zips:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                nome = Path(info.filename).name
                if info.is_dir() or Path(nome).suffix.lower() not in _SAD_EXTENSOES:
                    continue
                with zf.open(info) as origem, open(destino / nome, "wb") as saida:
                    shutil.copyfileobj(origem, saida, 1024 * 1024)

    camadas: List[CamadaSAD] = []
    for arquivo in sorted(destino.iterdir()):
        if arquivo.suffix.lower() not in (".shp", ".geojson"):
            continue
        info = _identificar_camada_sad(arquivo.name)
        if info is None:
            logging.warning("Arquivo fora do padrão do SAD, ignorado: %s", arquivo.name)
            continue
        tipo, camada, _periodos = info
        if arquivo.suffix.lower() == ".shp":
            faltando = [ext for ext in (".shx", ".dbf") if not arquivo.with_suffix(ext).exists()]
            if faltando:
                raise FileNotFoundError(
                    f"Shapefile {arquivo.name} incompleto (falta {', '.join(faltando)}). "
                    "Se o download veio dividido em várias partes, selecione todos os ZIPs."
                )
        if any(c.tipo == tipo and c.camada == camada for c in camadas):
            logging.warning("Camada %s/%s repetida, ignorando: %s", tipo, camada, arquivo.name)
            continue
        camadas.append(CamadaSAD(tipo, camada, arquivo))
    return camadas


def _ler_atributos_sad(camada: CamadaSAD) -> "pd.DataFrame":
    """
    Lê só a tabela de atributos (sem geometria) de uma camada, indexada pelo
    FID, com MES/ANO inteiros e a coluna de município padronizada.
    """
    _gpd, pd, pyogrio = _importar_libs_geo()
    df = pyogrio.read_dataframe(camada.path, read_geometry=False, fid_as_index=True)

    faltando = [col for col in ("MES", "ANO") if col not in df.columns]
    if faltando:
        raise ValueError(f"{camada.path.name} não tem a(s) coluna(s) {', '.join(faltando)}.")

    df = df.rename(columns=_SAD_RENOMEAR)
    for col in ("MES", "ANO"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    invalidos = df["MES"].isna() | df["ANO"].isna()
    if invalidos.any():
        logging.warning("%s: %d feição(ões) sem MES/ANO válido ignorada(s).",
                        camada.path.name, int(invalidos.sum()))
        df = df[~invalidos]
    return df.astype({"MES": int, "ANO": int})


def _csv_dashboard_sad(atributos: "pd.DataFrame", camada: str) -> "pd.DataFrame":
    """Atributos de uma camada no layout de colunas do CSV do dashboard."""
    df = atributos.rename(columns=_SAD_RENOMEAR_DASHBOARD)
    colunas = _SAD_COLUNAS_BASE + _SAD_COLUNAS_DASHBOARD[camada]
    # Colunas ausentes no arquivo recebido (ex.: a degradação em terra
    # indígena não traz município) ficam vazias.
    return df.reindex(columns=colunas, fill_value="")


def _mesclar_csv_dashboard_sad(novo: "pd.DataFrame", csv_s3: Optional[Path], meses) -> "pd.DataFrame":
    """
    Junta o CSV acumulado do S3 com os dados novos: remove do S3 as linhas
    dos meses enviados (`meses`, como AAAAMM) e acrescenta os novos. Os meses
    enviados são substituídos e o resto do histórico é mantido.
    """
    if csv_s3 is None:
        return novo
    _gpd, pd, _pyogrio = _importar_libs_geo()

    antigo = pd.read_csv(csv_s3, dtype=str, keep_default_na=False)
    if "MES" not in antigo.columns or "ANO" not in antigo.columns:
        logging.warning("CSV do S3 sem colunas MES/ANO: será substituído pelos dados do ZIP.")
        return novo

    mes = pd.to_numeric(antigo["MES"], errors="coerce")
    ano = pd.to_numeric(antigo["ANO"], errors="coerce")
    # Linhas sem MES/ANO válido (ex.: linhas corrompidas em versões antigas do
    # arquivo) não são lidas pelo dashboard e são descartadas.
    validos = mes.notna() & ano.notna()
    if not validos.all():
        logging.warning("CSV do S3: %d linha(s) sem MES/ANO válido descartada(s).",
                        int((~validos).sum()))
    periodo = (ano * 100 + mes).fillna(-1).astype("int64")
    substituir = periodo.isin(meses)
    manter = validos & ~substituir

    antigo = antigo[manter].copy()
    # Versões antigas do arquivo têm MES/ANO como "3.0"; padroniza para inteiro
    antigo["MES"] = mes[manter].astype(int).astype(str)
    antigo["ANO"] = ano[manter].astype(int).astype(str)

    logging.info("CSV do S3: %d linha(s) mantida(s), %d substituída(s) pelos meses enviados.",
                 len(antigo), int((validos & substituir).sum()))
    if antigo.empty:
        return novo
    antigo = antigo.reindex(columns=novo.columns, fill_value="")
    return pd.concat([antigo, novo], ignore_index=True)


def _zipar_diretorio(dir_origem: Path, output_zip: Path) -> None:
    """Compacta todos os arquivos (não recursivo) de dir_origem em output_zip."""
    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zout:
        for parte in sorted(dir_origem.iterdir()):
            if parte.is_file():
                zout.write(parte, parte.name)


def _enviar_arquivo_s3(s3_client, arquivo: Path, bucket: str, key: str, public: bool) -> None:
    """Envia um arquivo para o S3 e, se public, aplica ACL public-read."""
    logging.info("Enviando %s → s3://%s/%s …", arquivo.name, bucket, key)
    s3_client.upload_file(str(arquivo), bucket, key)
    logging.info("OK: s3://%s/%s", bucket, key)
    if public:
        s3_client.put_object_acl(Bucket=bucket, Key=key, ACL="public-read")
        logging.info("ACL public-read aplicada em s3://%s/%s", bucket, key)


def _atualizar_dashboard_sad(
    camadas: List[CamadaSAD],
    atributos: dict,
    bucket: str,
    s3_client,
    tmp: Path,
    public: bool,
    mes_unico: Optional[Tuple[int, int]] = None,
) -> None:
    """
    Atualiza os CSVs do dashboard, um por tipo + camada, mesclando com a
    versão acumulada que já está no S3:

        s3://bucket/dashboard/sad/csv/alertas_sad_{tipo}_{camada}.csv

    mes_unico=(ano, mes) envia só esse mês; None envia todos os dados do ZIP.
    """
    prefixo = _s3_dashboard_prefix(DATASETS["sad"].s3_root, "csv")
    for c in camadas:
        nome = f"alertas_sad_{c.tipo}_{_SAD_CSV_DASHBOARD[c.tipo][c.camada]}.csv"
        key = prefixo + nome
        df = atributos[(c.tipo, c.camada)]
        if mes_unico:
            ano, mes = mes_unico
            df = df[(df["ANO"] == ano) & (df["MES"] == mes)]
            # Mesmo sem alertas no mês, o mês é substituído no S3
            meses = [ano * 100 + mes]
        else:
            meses = (df["ANO"] * 100 + df["MES"]).unique()
        novo = _csv_dashboard_sad(df, c.camada)

        csv_s3 = tmp / f"s3_{nome}"
        baixou = baixar_arquivo_s3(s3_client, bucket, key, csv_s3)
        if mes_unico and not baixou:
            logging.warning("%s não existe no S3: será criado só com %02d/%d.",
                            nome, mes_unico[1], mes_unico[0])
        final = _mesclar_csv_dashboard_sad(novo, csv_s3 if baixou else None, meses)

        arquivo = tmp / nome
        final.to_csv(arquivo, index=False, encoding="utf-8")
        logging.info("%s: %d linha(s), %d do ZIP.", nome, len(final), len(novo))
        _enviar_arquivo_s3(s3_client, arquivo, bucket, key, public)
        csv_s3.unlink(missing_ok=True)
        arquivo.unlink(missing_ok=True)


def _publicar_mes_sad(
    camadas: List[CamadaSAD],
    atributos: dict,
    ano: int,
    mes: int,
    bucket: str,
    s3_client,
    tmp: Path,
    public: bool,
) -> None:
    """
    Gera e envia os arquivos de download de um mês: um ZIP por formato, com
    um arquivo por tipo + camada (alertas_sad_{tipo}_{MM}_{AAAA}_{camada}.{ext}):

        s3://bucket/sad/{geojson,csv,shapefile}/sad_{AAAA}_{MM}.zip
    """
    gpd, _pd, pyogrio = _importar_libs_geo()
    stem = f"sad_{ano}_{mes:02d}"
    dir_mes = tmp / stem
    dirs = {fmt: dir_mes / fmt for fmt in FORMATS}
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)

    alertas = 0
    for c in camadas:
        df = atributos[(c.tipo, c.camada)]
        sel = df[(df["ANO"] == ano) & (df["MES"] == mes)]
        if sel.empty:
            continue

        # Lê do arquivo só as geometrias das feições do mês, pelo FID
        geom = pyogrio.read_dataframe(c.path, fids=sel.index.to_numpy(), columns=[], fid_as_index=True)
        gdf = gpd.GeoDataFrame(sel.loc[geom.index], geometry=geom.geometry)

        nome = f"alertas_sad_{c.tipo}_{mes:02d}_{ano}_{c.camada}"
        pyogrio.write_dataframe(gdf, dirs["shapefile"] / f"{nome}.shp", encoding="UTF-8")
        pyogrio.write_dataframe(gdf.to_crs(epsg=4326) if c.reprojetar else gdf,
                                dirs["geojson"] / f"{nome}.geojson", driver="GeoJSON")
        sel.to_csv(dirs["csv"] / f"{nome}.csv", index=False, encoding="utf-8")
        alertas += len(sel)

    if alertas == 0:
        logging.warning("Nenhum alerta de %02d/%d no ZIP: arquivos mensais não gerados.", mes, ano)
    else:
        for fmt in FORMATS:
            arquivo = dir_mes / f"{stem}_{fmt}.zip"
            _zipar_diretorio(dirs[fmt], arquivo)
            key = f"{DATASETS['sad'].s3_root}/{fmt}/{stem}.zip"
            _enviar_arquivo_s3(s3_client, arquivo, bucket, key, public)
        logging.info("SAD %02d/%d: %d alerta(s) publicados.", mes, ano, alertas)
    shutil.rmtree(dir_mes, ignore_errors=True)


def processar_sad_zip(
    zips: List[Path],
    bucket: str,
    dry_run: bool,
    ano: Optional[int] = None,
    mes: Optional[int] = None,
    todos_meses: bool = False,
    public: bool = True,
    dashboard_todos_meses: bool = True,
) -> None:
    """
    Atualiza o SAD a partir do ZIP recebido, que traz um shapefile (ou
    GeoJSON) acumulado por tipo + camada, ex.:

        shapefile/alertas_sad_desmatamento_01_2008_07_2026_municipios.shp

    Aceita vários ZIPs quando o download vem dividido em partes.

    1. Dashboard — CSV de atributos de cada tipo + camada, mesclado com o
       acumulado do S3 (os meses enviados são substituídos):
           s3://bucket/dashboard/sad/csv/alertas_sad_{tipo}_{camada}.csv
       Envia todos os dados do ZIP ou, com dashboard_todos_meses=False, só
       o mês ano/mes.
    2. Download — um ZIP por mês e formato, com um arquivo por tipo + camada:
           s3://bucket/sad/{geojson,csv,shapefile}/sad_{AAAA}_{MM}.zip
       Só para o mês ano/mes (se omitidos, o último mês do nome dos
       arquivos) ou, com todos_meses=True, para cada mês com alertas no ZIP.
    """
    zips = [Path(z) for z in zips]
    if not zips:
        raise ValueError("Nenhum ZIP do SAD informado.")
    for zip_path in zips:
        if not zip_path.is_file():
            raise FileNotFoundError(f"ZIP do SAD não encontrado: {zip_path}")

    resumo = inspecionar_zips_sad(zips)
    if not resumo["camadas"]:
        raise ValueError(
            "Nenhuma camada do SAD encontrada no(s) ZIP(s). Esperado: "
            "alertas_sad_{tipo}_{MM}_{AAAA}_..._{camada}.shp"
        )
    if (not todos_meses or not dashboard_todos_meses) and (ano is None or mes is None):
        ano, mes = resumo["fim"]

    logging.info("=== SAD: %s ===", ", ".join(z.name for z in zips))
    logging.info("Camadas: %s", ", ".join(f"{t}/{c}" for t, c in resumo["camadas"]))
    logging.info("CSVs do dashboard: %s",
                 "todos os dados do ZIP" if dashboard_todos_meses else f"só {mes:02d}/{ano}")
    logging.info("Arquivos mensais: %s", "todos os meses do ZIP" if todos_meses else f"{mes:02d}/{ano}")

    prefixo_dashboard = _s3_dashboard_prefix(DATASETS["sad"].s3_root, "csv")
    s3_root = DATASETS["sad"].s3_root

    if dry_run:
        substituidos = "os meses do ZIP" if dashboard_todos_meses else f"{mes:02d}/{ano}"
        for tipo, camada in resumo["camadas"]:
            key = f"{prefixo_dashboard}alertas_sad_{tipo}_{_SAD_CSV_DASHBOARD[tipo][camada]}.csv"
            logging.info("[DRY RUN] Baixaria s3://%s/%s, substituiria %s e reenviaria",
                         bucket, key, substituidos)
        if todos_meses:
            (ano_ini, mes_ini), (ano_fim, mes_fim) = resumo["inicio"], resumo["fim"]
            logging.info(
                "[DRY RUN] Enviaria s3://%s/%s/{geojson,csv,shapefile}/sad_AAAA_MM.zip "
                "para cada mês com alertas entre %02d/%d e %02d/%d",
                bucket, s3_root, mes_ini, ano_ini, mes_fim, ano_fim,
            )
        else:
            for fmt in FORMATS:
                logging.info("[DRY RUN] Enviaria s3://%s/%s/%s/sad_%d_%02d.zip",
                             bucket, s3_root, fmt, ano, mes)
        if public:
            logging.info("[DRY RUN] Definiria ACL public-read em todos os objetos enviados")
        return

    _gpd, _pd, pyogrio = _importar_libs_geo()
    s3_client = create_s3_client()

    with tempfile.TemporaryDirectory(prefix="imazon_sad_", ignore_cleanup_errors=True) as tmp_str:
        tmp = Path(tmp_str)
        dir_extraido = tmp / "extraido"
        dir_extraido.mkdir()
        logging.info("Extraindo ZIP(s) em %s …", dir_extraido)
        camadas = _extrair_zips_sad(zips, dir_extraido)

        atributos = {}
        for c in camadas:
            c.reprojetar = pyogrio.read_info(c.path)["crs"] not in (None, "EPSG:4326")
            atributos[(c.tipo, c.camada)] = _ler_atributos_sad(c)
            logging.info("%s/%s: %d feição(ões)", c.tipo, c.camada, len(atributos[(c.tipo, c.camada)]))

        logging.info(">>> Dashboard: s3://%s/%s", bucket, prefixo_dashboard)
        _atualizar_dashboard_sad(camadas, atributos, bucket, s3_client, tmp, public,
                                 mes_unico=None if dashboard_todos_meses else (ano, mes))

        if todos_meses:
            meses = sorted({
                (int(a), int(m))
                for df in atributos.values()
                for a, m in df[["ANO", "MES"]].drop_duplicates().itertuples(index=False)
            })
        else:
            meses = [(ano, mes)]

        logging.info(">>> Download: %d mês(es) em s3://%s/%s/{geojson,csv,shapefile}/",
                     len(meses), bucket, s3_root)
        for i, (a, m) in enumerate(meses, start=1):
            logging.info("[%d/%d] SAD %02d/%d", i, len(meses), m, a)
            _publicar_mes_sad(camadas, atributos, a, m, bucket, s3_client, tmp, public)


_EXT_TO_FORMAT = {".zip": "shapefile", ".csv": "csv", ".geojson": "geojson"}

# Extensões aceitas para o arquivo escolhido pelo usuário, por formato
_EXTENSOES_ARQUIVO = {"shapefile": (".zip",), "csv": (".csv",), "geojson": (".geojson", ".json")}


def _arquivo_com_nome_padrao(
    arquivo: Path,
    cfg: DatasetConfig,
    periodo: dict,
    formats: Optional[List[str]],
    op: str,
    tmp: Path,
) -> Tuple[Path, str]:
    """
    Copia o arquivo escolhido pelo usuário para `tmp` com o nome padrão do
    dataset e período (ex.: floreser_2025.geojson), que é o nome usado no S3,
    e retorna (caminho, prefixo_s3).
    """
    if not formats or len(formats) != 1:
        raise ValueError("Informe exatamente um formato para o arquivo escolhido.")
    fmt = formats[0]
    if not arquivo.is_file():
        raise FileNotFoundError(f"Arquivo não encontrado: {arquivo}")
    if arquivo.suffix.lower() not in _EXTENSOES_ARQUIVO[fmt]:
        raise ValueError(
            f"{arquivo.name} não é um arquivo {fmt} "
            f"(esperado {' ou '.join(_EXTENSOES_ARQUIVO[fmt])})."
        )

    nome = montar_nome_arquivo(cfg, periodo, fmt)
    destino = tmp / nome
    shutil.copy2(arquivo, destino)
    logging.info("Arquivo escolhido: %s (enviado como %s)", arquivo, nome)

    if op == "dashboard":
        return destino, _prefixo_dashboard(cfg, fmt)
    return destino, f"{cfg.s3_root}/{fmt}/"


def processar_dataset(
    nome_dataset: str,
    base_dir: Path,
    bucket: str,
    year: Optional[int],
    month: Optional[int],
    quarter: Optional[int],
    dry_run: bool,
    public: bool,
    formats: Optional[List[str]] = None,
    concatenar: bool = False,
    op: str = "download",
    arquivo: Optional[Path] = None,
) -> None:
    """
    Processa Floreser, Ameaça & Pressão e SIMEX (o SAD tem fluxo próprio:
    processar_sad_zip).

    arquivo: arquivo escolhido pelo usuário (um só formato em `formats`); é
             enviado com o nome padrão do período. Se omitido, os arquivos
             são procurados em {base_dir}/{dataset}/...

    op="download"  → upload direto ao prefixo {s3_root}/{fmt}/
    op="dashboard" → concatena com S3 e envia ao prefixo de dashboard,
                     salvando cópia local em {base_dir}/dashboard/{s3_root}/{fmt}/

    formats:    subconjunto de FORMATS a enviar; None = todos.
    concatenar: ignorado quando op="dashboard" (sempre concatena).
    """
    cfg = DATASETS[nome_dataset]
    logging.info("=== Processando %s (%s) [op=%s] ===", cfg.descricao, cfg.slug, op)

    # SAD: fluxo próprio, a partir do ZIP recebido
    if cfg.slug == "sad":
        logging.warning(
            "O SAD é atualizado a partir do ZIP recebido: use processar_sad_zip "
            "(na linha de comando, --zip)."
        )
        return

    # A&P e SIMEX têm dashboard próprio, no formato lido pelos dashboards
    if op == "dashboard" and cfg.slug in ("ameaca_pressao", "simex"):
        logging.warning("O dashboard de %s tem fluxo próprio: use processar_%s_dashboard.",
                        cfg.descricao, cfg.slug)
        return

    periodo = descobrir_periodo(cfg, year, month, quarter)
    logging.info("Período detectado: %s", periodo)

    if arquivo is not None:
        with tempfile.TemporaryDirectory(prefix="imazon_arquivo_") as tmp:
            arquivos = [_arquivo_com_nome_padrao(Path(arquivo), cfg, periodo, formats, op, Path(tmp))]
            _enviar_arquivos_dataset(arquivos, cfg, base_dir, bucket, dry_run, public, concatenar, op)
        return

    arquivos = coletar_arquivos_multi_tipo(base_dir, cfg, periodo, formats=formats, op=op)

    if not arquivos:
        logging.warning("Nenhum arquivo encontrado para %s no período %s.", cfg.slug, periodo)
        return

    _enviar_arquivos_dataset(arquivos, cfg, base_dir, bucket, dry_run, public, concatenar, op)


def _enviar_arquivos_dataset(
    arquivos: List[Tuple[Path, str]],
    cfg: DatasetConfig,
    base_dir: Path,
    bucket: str,
    dry_run: bool,
    public: bool,
    concatenar: bool,
    op: str,
) -> None:
    """Envia os arquivos de um dataset; no dashboard (ou com concatenar), junta antes com o S3."""
    deve_concatenar = concatenar or (op == "dashboard")

    if deve_concatenar:
        if dry_run:
            for arq, prefixo_s3 in arquivos:
                key = prefixo_s3 + arq.name
                logging.info(
                    "[DRY RUN] Baixaria s3://%s/%s, concatenaria com %s e reenviaria",
                    bucket, key, arq,
                )
        else:
            s3_client = create_s3_client()
            with tempfile.TemporaryDirectory() as tmp:
                arquivos_merged = concatenar_com_s3(arquivos, bucket, s3_client, Path(tmp))

                # Para dashboard, persiste cópia local em {base_dir}/dashboard/{s3_root}/{fmt}/
                if op == "dashboard":
                    for merged_path, _ in arquivos_merged:
                        fmt_key = _EXT_TO_FORMAT.get(merged_path.suffix.lower(),
                                                      merged_path.suffix.lstrip("."))
                        local_dir = base_dir / "dashboard" / cfg.s3_root / fmt_key
                        local_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(merged_path, local_dir / merged_path.name)
                        logging.info("Salvo localmente: %s", local_dir / merged_path.name)

                enviar_arquivos_para_s3_multi(
                    arquivos_merged,
                    bucket=bucket,
                    dry_run=False,
                    public=public,
                )
        return

    enviar_arquivos_para_s3_multi(
        arquivos,
        bucket=bucket,
        dry_run=dry_run,
        public=public,
    )


# ----------------------------------------------------------------------
# Ameaça & Pressão: dashboard (arquivos anuais por categoria)
# ----------------------------------------------------------------------

_AP_EXTENSOES = (".geojson", ".json", ".zip")


def _localizar_zip_ap(base_dir: Path, year: int) -> Optional[Path]:
    """
    Localiza o ZIP anual de Ameaça & Pressão.
    Procura em dados/ameaca_pressao/AAAA/ e subpastas.
    """
    raiz = base_dir / "ameaca_pressao" / str(year)
    padrao = f"ameaca_e_pressao_{year}.zip"

    candidato = raiz / padrao
    if candidato.exists():
        return candidato

    if raiz.exists():
        for sub in raiz.iterdir():
            if sub.is_dir():
                c = sub / padrao
                if c.exists():
                    return c
        zips = list(raiz.glob("*.zip"))
        if zips:
            return zips[0]

    return None


def _ano_trimestre_ap(feat: dict) -> Optional[Tuple[int, int]]:
    """(ano, trimestre) de uma feição de ameaça e pressão, ou None se faltar."""
    props = feat.get("properties") or {}
    try:
        return int(props["ano"]), int(props["trimestre"])
    except (KeyError, TypeError, ValueError):
        return None


def _ler_geojsons_ap(arquivo: Path) -> Tuple[List[dict], Optional[dict]]:
    """
    Feições de um GeoJSON ou de todos os GeoJSONs de um ZIP, junto com o `crs`
    do primeiro arquivo que tiver um.
    """
    if arquivo.suffix.lower() == ".zip":
        with zipfile.ZipFile(arquivo) as zf:
            colecoes = [json.loads(zf.read(n).decode("utf-8-sig"))
                        for n in zf.namelist() if n.lower().endswith((".geojson", ".json"))]
    else:
        with open(arquivo, encoding="utf-8-sig") as f:
            colecoes = [json.load(f)]
    features = [feat for colecao in colecoes for feat in colecao.get("features", [])]
    crs = next((colecao["crs"] for colecao in colecoes if "crs" in colecao), None)
    return features, crs


def processar_ameaca_pressao_dashboard(
    base_dir: Path,
    bucket: str,
    year: int,
    dry_run: bool,
    public: bool = True,
    arquivo: Optional[Path] = None,
) -> None:
    """
    Dashboard de Ameaça & Pressão: um arquivo anual por categoria (dado) e
    classe (ameaça/pressão), com os trimestres do ano:

        s3://bucket/dashboard/ap/geojson/ameaca_e_pressao_{ano}_{dado}_{classe}.geojson

    Lê as feições do GeoJSON do trimestre (ou dos GeoJSONs de um ZIP), agrupa
    por ano + dado + classe e, em cada arquivo anual do S3, substitui os
    trimestres enviados e mantém os demais.

    arquivo: GeoJSON ou ZIP escolhido; se omitido, é usado o ZIP em
             {base_dir}/ameaca_pressao/AAAA/.
    """
    cfg = DATASETS["ameaca_pressao"]
    logging.info("=== Dashboard Ameaça & Pressão ===")

    if arquivo is None:
        arquivo = _localizar_zip_ap(base_dir, year)
        if arquivo is None:
            logging.warning("ZIP de Ameaça & Pressão não encontrado em %s/ameaca_pressao/%d/.",
                            base_dir, year)
            return
    arquivo = Path(arquivo)
    if not arquivo.is_file():
        raise FileNotFoundError(f"Arquivo de Ameaça & Pressão não encontrado: {arquivo}")
    if arquivo.suffix.lower() not in _AP_EXTENSOES:
        raise ValueError("No dashboard de Ameaça & Pressão, envie o GeoJSON do trimestre ou um ZIP com GeoJSONs.")
    logging.info("Arquivo: %s", arquivo)

    features, crs = _ler_geojsons_ap(arquivo)
    grupos: "dict[Tuple[int, str, str], List[dict]]" = {}
    ignoradas = 0
    for feat in features:
        props = feat.get("properties") or {}
        periodo = _ano_trimestre_ap(feat)
        if periodo is None or not props.get("dado") or not props.get("class"):
            ignoradas += 1
            continue
        ano, trimestre = periodo
        props.setdefault("_source_file", f"ameaca_e_pressao_{trimestre}_trimestre_{ano}.geojson")
        grupos.setdefault((ano, str(props["dado"]), str(props["class"])), []).append(feat)
    if ignoradas:
        logging.warning("%d feição(ões) sem ano, trimestre, dado ou class ignorada(s).", ignoradas)
    if not grupos:
        raise ValueError(f"Nenhuma feição de ameaça e pressão (com ano, trimestre, dado e class) em {arquivo.name}.")

    prefixo = _prefixo_dashboard(cfg, "geojson")
    s3_client = None if dry_run else create_s3_client()

    with tempfile.TemporaryDirectory(prefix="imazon_ap_") as tmp_str:
        tmp = Path(tmp_str)
        for (ano, dado, classe), novas in sorted(grupos.items()):
            nome = f"ameaca_e_pressao_{ano}_{dado}_{classe}.geojson"
            key = prefixo + nome
            enviados = {_ano_trimestre_ap(f) for f in novas}
            trimestres = ", ".join(str(t) for _a, t in sorted(enviados))
            if dry_run:
                logging.info("[DRY RUN] %s: substituiria o(s) trimestre(s) %s (%d feições) em s3://%s/%s",
                             nome, trimestres, len(novas), bucket, key)
                continue

            s3_atual = tmp / f"s3_{nome}"
            if baixar_arquivo_s3(s3_client, bucket, key, s3_atual):
                with open(s3_atual, encoding="utf-8-sig") as f:
                    colecao = json.load(f)
            else:
                colecao = {"type": "FeatureCollection", "name": Path(nome).stem, "features": []}
                if crs:
                    colecao["crs"] = crs

            antigas = colecao.get("features", [])
            mantidas = [f for f in antigas if _ano_trimestre_ap(f) not in enviados]
            # Mantém os trimestres em ordem dentro do arquivo anual
            colecao["features"] = sorted(mantidas + novas, key=lambda f: _ano_trimestre_ap(f) or (0, 0))
            logging.info("%s: trimestre(s) %s — %d feição(ões) mantida(s), %d substituída(s), %d nova(s).",
                         nome, trimestres, len(mantidas), len(antigas) - len(mantidas), len(novas))

            arquivo_final = tmp / nome
            with open(arquivo_final, "w", encoding="utf-8") as f:
                json.dump(colecao, f, ensure_ascii=False)
            _enviar_arquivo_s3(s3_client, arquivo_final, bucket, key, public)


# ----------------------------------------------------------------------
# SIMEX: dashboard (arquivos por camada, com todos os anos)
# ----------------------------------------------------------------------

# Camada no arquivo unificado do ano (início do nome em origem_arquivo ou
# source_layer, em minúsculas) → (nome no CSV, nome no GeoJSON) do dashboard.
# A ordem importa: "municipios" antes de "mun".
_SIMEX_CAMADAS = [
    ("imoveisrurais", "imoveisrurais", "imoveisrurais"),
    ("municipios",    "municipios",    "mun"),
    ("mun",           "municipios",    "mun"),
    ("assentamentos", "assentamentos", "assentamentos"),
    ("terraspndest",  "terras_ndest",  "TerrasNDest"),
    ("terrasndest",   "terras_ndest",  "TerrasNDest"),
    ("terras_ndest",  "terras_ndest",  "TerrasNDest"),
    ("ti",            "ti",            "TI"),
    ("uc",            "uc",            "UC"),
]

# Colunas que só identificam a origem de cada registro no arquivo unificado
_SIMEX_COLUNAS_ORIGEM = ("origem_arquivo", "origem_planilha", "source_layer")


def _camada_simex(registro) -> Optional[Tuple[str, str]]:
    """
    Camada do dashboard de um registro (dict ou linha) do arquivo unificado do
    SIMEX, pela coluna origem_arquivo (ex.: simex_amz_2024_imoveisruraispri.xlsx)
    ou source_layer (ex.: TerrasNDest). Retorna (nome_csv, nome_geojson) ou None.
    """
    for campo in ("origem_arquivo", "source_layer"):
        valor = registro.get(campo)
        if not isinstance(valor, str) or not valor.strip():
            continue
        token = re.sub(r"^simex_amz(?:onia)?_(?:\d{4}_)?", "", Path(valor.strip()).stem.lower())
        for inicio, nome_csv, nome_geojson in _SIMEX_CAMADAS:
            if token.startswith(inicio):
                return nome_csv, nome_geojson
    return None


def _ano_simex(registro) -> Optional[int]:
    """Ano de um registro (dict ou linha), pela coluna ano, Ano ou ANO."""
    for campo in ("ano", "Ano", "ANO"):
        try:
            return int(float(registro.get(campo)))
        except (TypeError, ValueError):
            continue
    return None


def processar_simex_dashboard(
    bucket: str,
    dry_run: bool,
    arquivo: Path,
    public: bool = True,
) -> None:
    """
    Dashboard do SIMEX: arquivos por camada (assentamentos, imóveis rurais,
    municípios, terras não destinadas, TI e UC), cada um com todos os anos:

        CSV     → s3://bucket/dashboard/simex/csv/simex_amazonia_PAMT_{camada}.csv
        GeoJSON → s3://bucket/dashboard/simex/geojson/simex_amz_PAMTM_{camada}.geojson

    Recebe o arquivo unificado do ano (o mesmo do download) em CSV ou GeoJSON,
    separa os registros por camada (origem_arquivo ou source_layer) e, em cada
    arquivo do S3, substitui os anos enviados e mantém os demais. O CSV
    atualiza os CSVs do dashboard; o GeoJSON, os mapas.
    """
    cfg = DATASETS["simex"]
    arquivo = Path(arquivo)
    if not arquivo.is_file():
        raise FileNotFoundError(f"Arquivo do SIMEX não encontrado: {arquivo}")
    ext = arquivo.suffix.lower()
    if ext not in (".csv", ".geojson", ".json"):
        raise ValueError("No dashboard do SIMEX, envie o arquivo unificado do ano em CSV ou GeoJSON.")
    logging.info("=== Dashboard SIMEX: %s ===", arquivo.name)

    s3_client = None if dry_run else create_s3_client()
    with tempfile.TemporaryDirectory(prefix="imazon_simex_") as tmp_str:
        if ext == ".csv":
            _simex_dashboard_csv(cfg, arquivo, bucket, s3_client, Path(tmp_str), dry_run, public)
        else:
            _simex_dashboard_geojson(cfg, arquivo, bucket, s3_client, Path(tmp_str), dry_run, public)


def _simex_dashboard_csv(cfg: DatasetConfig, arquivo: Path, bucket: str, s3_client,
                         tmp: Path, dry_run: bool, public: bool) -> None:
    """CSVs do dashboard do SIMEX a partir do CSV unificado do ano."""
    import pandas as pd

    df = pd.read_csv(arquivo, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    registros = df.to_dict("records")
    camadas = pd.Series([_camada_simex(r) for r in registros], index=df.index, dtype=object)
    anos = pd.Series([_ano_simex(r) for r in registros], index=df.index, dtype=object)
    sem_camada = int(camadas.isna().sum())
    if sem_camada:
        logging.warning("%d linha(s) sem camada reconhecida (origem_arquivo/source_layer) ignorada(s).", sem_camada)
    if sem_camada == len(df):
        raise ValueError(f"{arquivo.name}: nenhuma linha com camada reconhecida em origem_arquivo ou source_layer.")

    prefixo = _prefixo_dashboard(cfg, "csv")
    for nome_csv in sorted({c[0] for c in camadas.dropna()}):
        mascara = camadas.map(lambda c: c is not None and c[0] == nome_csv).astype(bool)
        novos = df[mascara]
        # Só as colunas preenchidas na camada, sem as de origem do arquivo unificado
        colunas = [c for c in novos.columns if c not in _SIMEX_COLUNAS_ORIGEM and (novos[c] != "").any()]
        novos = novos[colunas].assign(__source_file=arquivo.name)
        anos_enviados = sorted({a for a in anos[mascara] if a is not None})

        nome = f"simex_amazonia_PAMT_{nome_csv}.csv"
        key = prefixo + nome
        if dry_run:
            logging.info("[DRY RUN] %s: substituiria o(s) ano(s) %s (%d linhas) em s3://%s/%s",
                         nome, anos_enviados, len(novos), bucket, key)
            continue

        csv_s3 = tmp / f"s3_{nome}"
        if baixar_arquivo_s3(s3_client, bucket, key, csv_s3):
            antigo = pd.read_csv(csv_s3, dtype=str, keep_default_na=False, encoding="utf-8-sig")
            anos_antigo = pd.Series([_ano_simex(r) for r in antigo.to_dict("records")],
                                    index=antigo.index, dtype=object)
            manter = ~anos_antigo.isin(anos_enviados)
            final = pd.concat([antigo[manter], novos], ignore_index=True)
            logging.info("%s: ano(s) %s — %d linha(s) mantida(s), %d substituída(s), %d nova(s).",
                         nome, anos_enviados, int(manter.sum()), int((~manter).sum()), len(novos))
        else:
            final = novos
            logging.info("%s não existe no S3: será criado com %d linha(s).", nome, len(novos))

        arquivo_final = tmp / nome
        final.to_csv(arquivo_final, index=False, encoding="utf-8-sig")
        _enviar_arquivo_s3(s3_client, arquivo_final, bucket, key, public)
        csv_s3.unlink(missing_ok=True)


def _simex_dashboard_geojson(cfg: DatasetConfig, arquivo: Path, bucket: str, s3_client,
                             tmp: Path, dry_run: bool, public: bool) -> None:
    """GeoJSONs (mapas) do dashboard do SIMEX a partir do GeoJSON unificado do ano."""
    with open(arquivo, encoding="utf-8-sig") as f:
        entrada = json.load(f)

    grupos: "dict[str, List[dict]]" = {}
    ignoradas = 0
    for feat in entrada.get("features", []):
        props = feat.get("properties") or {}
        camada = _camada_simex(props)
        if camada is None:
            ignoradas += 1
            continue
        feat["properties"] = {k: v for k, v in props.items() if k not in _SIMEX_COLUNAS_ORIGEM}
        grupos.setdefault(camada[1], []).append(feat)
    if ignoradas:
        logging.warning("%d feição(ões) sem camada reconhecida (origem_arquivo/source_layer) ignorada(s).", ignoradas)
    if not grupos:
        raise ValueError(f"{arquivo.name}: nenhuma feição com camada reconhecida em origem_arquivo ou source_layer.")

    prefixo = _prefixo_dashboard(cfg, "geojson")
    for nome_geojson, novas in sorted(grupos.items()):
        nome = f"simex_amz_PAMTM_{nome_geojson}.geojson"
        key = prefixo + nome
        anos_enviados = sorted({a for a in (_ano_simex(f["properties"]) for f in novas) if a is not None})
        if dry_run:
            logging.info("[DRY RUN] %s: substituiria o(s) ano(s) %s (%d feições) em s3://%s/%s",
                         nome, anos_enviados, len(novas), bucket, key)
            continue

        s3_atual = tmp / f"s3_{nome}"
        if baixar_arquivo_s3(s3_client, bucket, key, s3_atual):
            with open(s3_atual, encoding="utf-8-sig") as f:
                colecao = json.load(f)
        else:
            colecao = {"type": "FeatureCollection", "features": []}

        antigas = colecao.get("features", [])
        # Feições sem ano não podem ser associadas a um envio e são mantidas
        mantidas = [f for f in antigas if _ano_simex(f.get("properties") or {}) not in anos_enviados]
        colecao["features"] = mantidas + novas
        logging.info("%s: ano(s) %s — %d feição(ões) mantida(s), %d substituída(s), %d nova(s).",
                     nome, anos_enviados, len(mantidas), len(antigas) - len(mantidas), len(novas))

        arquivo_final = tmp / nome
        with open(arquivo_final, "w", encoding="utf-8") as f:
            json.dump(colecao, f, ensure_ascii=False)
        _enviar_arquivo_s3(s3_client, arquivo_final, bucket, key, public)
        s3_atual.unlink(missing_ok=True)
        arquivo_final.unlink(missing_ok=True)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload / simulação de SAD / Floreser / "
                    "Ameaça & Pressão / SIMEX (ZIPs por tipo) para o S3."
    )

    parser.add_argument(
        "dataset",
        choices=list(DATASETS.keys()) + ["all"],
        help="Qual dataset processar: "
             "'sad', 'floreser', 'ameaca_pressao', 'simex' ou 'all' para todos.",
    )

    parser.add_argument(
        "--bucket",
        required=True,
        help="Nome do bucket S3 de destino (ex: imazongeo3-web).",
    )

    parser.add_argument(
        "--base-dir",
        default="dados",
        help="Diretório base onde estão as pastas 'sad', 'floreser', "
             "'ameaca_pressao', 'simex'. (default: 'dados')",
    )

    parser.add_argument(
        "--year",
        type=int,
        help="Ano de referência (ex: 2025). Se omitido, usa o ano atual.",
    )

    parser.add_argument(
        "--month",
        type=int,
        choices=range(1, 13),
        help="SAD: mês dos arquivos mensais (1-12), junto com --year. "
             "Se omitido, usa o último mês do ZIP.",
    )

    parser.add_argument(
        "--quarter",
        type=int,
        choices=range(1, 5),
        help="Trimestre para Ameaça & Pressão (1-4). "
             "Se omitido, é calculado a partir do mês atual.",
    )

    parser.add_argument(
        "--zip",
        dest="zips",
        action="append",
        type=Path,
        help="SAD: ZIP recebido. Repita a opção se o download veio dividido "
             "em partes (ex.: --zip parte1.zip --zip parte2.zip).",
    )

    parser.add_argument(
        "--todos-meses",
        action="store_true",
        help="SAD: gera os arquivos mensais de download para todos os meses "
             "do ZIP (por padrão, só --year/--month ou o último mês do ZIP).",
    )

    parser.add_argument(
        "--dashboard-so-mes",
        action="store_true",
        help="SAD: nos CSVs do dashboard, envia só o mês (--year/--month ou o "
             "último mês do ZIP) em vez de todos os dados do ZIP.",
    )

    parser.add_argument(
        "--no-dry-run",
        dest="dry_run",
        action="store_false",
        help="Se informado, faz o upload REAL para o S3 (usa boto3). "
             "Por padrão, apenas simula.",
    )

    parser.add_argument(
        "--public",
        action="store_true",
        help="Se informado junto com --no-dry-run, define ACL public-read "
             "em cada objeto enviado (necessita bucket/política permitindo "
             "acesso público).",
    )

    parser.add_argument(
        "--concatenar",
        action="store_true",
        help="Baixa o arquivo existente no S3, concatena com o local e envia o resultado "
             "(acumula dados). Requer --no-dry-run para executar de fato.",
    )

    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Modo verboso (logs em DEBUG).",
    )

    parser.set_defaults(dry_run=True)

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging(args.verbose)

    base_dir = Path(args.base_dir).resolve()
    logging.info("Base dir: %s", base_dir)
    logging.info("Bucket: %s", args.bucket)
    logging.info("Dry-run: %s", args.dry_run)
    logging.info("Public: %s", args.public)

    nomes = list(DATASETS.keys()) if args.dataset == "all" else [args.dataset]
    for nome in nomes:
        if nome == "sad":
            if args.zips:
                processar_sad_zip(
                    zips=args.zips,
                    bucket=args.bucket,
                    dry_run=args.dry_run,
                    ano=args.year,
                    mes=args.month,
                    todos_meses=args.todos_meses,
                    dashboard_todos_meses=not args.dashboard_so_mes,
                    public=args.public,
                )
            else:
                logging.warning("SAD ignorado: informe o ZIP recebido com --zip.")
            continue

        processar_dataset(
            nome_dataset=nome,
            base_dir=base_dir,
            bucket=args.bucket,
            year=args.year,
            month=args.month,
            quarter=args.quarter,
            dry_run=args.dry_run,
            public=args.public,
            concatenar=args.concatenar,
        )


if __name__ == "__main__":
    main()
