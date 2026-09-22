"""Definição dos datasets, períodos, nomes de arquivo e prefixos no S3."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

FORMATS = ["shapefile", "csv", "geojson"]

# Extensão do arquivo enviado, por formato
FORMAT_EXT = {
    "csv": ".csv",
    "shapefile": ".zip",
    "geojson": ".geojson",
}

# Prefixo base para uploads de dashboard
S3_DASHBOARD_BASE = "dashboard"


@dataclass
class DatasetConfig:
    """Configuração de um dataset."""

    slug: str  # 'sad', 'floreser', 'ameaca_pressao', 'simex'
    descricao: str  # descrição amigável
    frequencia: str  # 'monthly', 'quarterly', 'annual'
    local_subdir: str  # subpasta dentro de base_dir
    s3_root: str  # raiz dentro do bucket (ex.: 'sad', 'ameaca_e_pressao')
    # Pasta em dashboard/ lida pelo dashboard, se diferente de s3_root
    dashboard_root: str = ""


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


def descobrir_periodo(
    cfg: DatasetConfig,
    year: int | None,
    month: int | None,
    quarter: int | None,
) -> dict:
    """Descobre ano/mês/trimestre com base no que foi passado + data atual."""
    hoje = date.today()
    ano = year or hoje.year

    if cfg.frequencia == "monthly":
        mes = month or hoje.month
        return {"year": ano, "month": mes}
    if cfg.frequencia == "quarterly":
        q = quarter or (hoje.month - 1) // 3 + 1
        return {"year": ano, "quarter": q}
    return {"year": ano}  # annual


def montar_nome_arquivo(cfg: DatasetConfig, periodo: dict, tipo: str = "") -> str:
    """Monta o nome do arquivo de acordo com o dataset e o período.

    - SAD:              alertas_sad_MM_YYYY.{ext}
    - SIMEX (anual):    simex_unificado_YYYY.{ext}
    - Floreser (anual): floreser_YYYY.{ext}
    - A&P (trimestral): ameaca_e_pressao_{Q}_trimestre_YYYY.{ext}
    """
    year = periodo["year"]
    ext = FORMAT_EXT.get(tipo, ".zip")

    if cfg.slug == "sad":
        month = periodo["month"]
        return f"alertas_sad_{month:02d}_{year}{ext}"
    if cfg.slug == "simex":
        return f"simex_unificado_{year}{ext}"
    if cfg.slug == "floreser":
        return f"floreser_{year}{ext}"
    if cfg.slug == "ameaca_pressao":
        q = periodo["quarter"]
        return f"ameaca_e_pressao_{q}_trimestre_{year}{ext}"
    raise ValueError(f"Dataset não suportado: {cfg.slug}")


def montar_diretorio_base_local(
    base_dir: Path, cfg: DatasetConfig, periodo: dict
) -> Path:
    """Diretório local (sem o /<tipo>) onde ficam os arquivos do dataset."""
    root = base_dir / cfg.local_subdir

    if cfg.frequencia == "monthly":
        return root / f"{periodo['year']}" / f"{periodo['month']:02d}"
    if cfg.frequencia == "quarterly":
        return root / f"{periodo['year']}" / f"T{periodo['quarter']}"
    return root / f"{periodo['year']}"  # annual


def s3_dashboard_prefix(dataset_s3_root: str, fmt: str) -> str:
    """Prefixo S3 de dashboard: ``{S3_DASHBOARD_BASE}/{dataset}/{fmt}/``.

    Com S3_DASHBOARD_BASE vazio, o prefixo fica ``{dataset}/{fmt}/``.
    """
    base = S3_DASHBOARD_BASE.rstrip("/")
    if base:
        return f"{base}/{dataset_s3_root}/{fmt}/"
    return f"{dataset_s3_root}/{fmt}/"


def prefixo_dashboard(cfg: DatasetConfig, fmt: str) -> str:
    """Prefixo de dashboard de um dataset (A&P usa a pasta 'ap')."""
    return s3_dashboard_prefix(cfg.dashboard_root or cfg.s3_root, fmt)
