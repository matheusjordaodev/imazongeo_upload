"""Linha de comando: upload (ou simulação) dos datasets para o S3.

Todos os arquivos seguem o padrão de nome ``nome_ano_mes.extensão``
(ex.: sad_2025_11.zip) e são enviados em 3 formatos (shapefile, csv,
geojson) para subpastas no S3.

Uso básico::

  # SAD: simulação (dry-run, default) a partir do ZIP recebido
  imazongeo-upload sad --bucket imazongeo3-web --zip shapefile.zip

  # SAD: upload real + objetos públicos (ACL public-read), com o download
  # dividido em partes e arquivos mensais de todos os meses do ZIP
  imazongeo-upload sad --bucket imazongeo3-web --zip parte1.zip \\
      --zip parte2.zip --todos-meses --no-dry-run --public

  # Demais datasets (arquivos em dados/{dataset}/...)
  imazongeo-upload simex --bucket imazongeo3-web --year 2025 --no-dry-run
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path

from .config import load_env, setup_logging
from .datasets import DATASETS
from .processing import processar_dataset
from .sad import processar_sad_zip


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Interpreta os argumentos da linha de comando."""
    parser = argparse.ArgumentParser(
        prog="imazongeo-upload",
        description="Upload / simulação de SAD / Floreser / "
        "Ameaça & Pressão / SIMEX (ZIPs por tipo) para o S3.",
    )

    parser.add_argument(
        "dataset",
        choices=[*DATASETS, "all"],
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
        help="Baixa o arquivo existente no S3, concatena com o local e envia o "
        "resultado (acumula dados). Requer --no-dry-run para executar de fato.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Modo verboso (logs em DEBUG).",
    )
    parser.set_defaults(dry_run=True)

    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """Ponto de entrada do comando ``imazongeo-upload``."""
    load_env()
    args = parse_args(argv)
    setup_logging(args.verbose)

    base_dir = Path(args.base_dir).resolve()
    logging.info("Base dir: %s", base_dir)
    logging.info("Bucket: %s", args.bucket)
    logging.info("Dry-run: %s", args.dry_run)
    logging.info("Public: %s", args.public)

    nomes = list(DATASETS) if args.dataset == "all" else [args.dataset]
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
