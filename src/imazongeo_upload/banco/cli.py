"""Comando ``imazongeo-banco``: banco de dados como fonte do S3.

Exemplos::

  imazongeo-banco criar-schema                     # todos os datasets
  imazongeo-banco criar-schema --dataset sad
  # SAD: arquivos acumulados por tipo/camada (ZIP, GeoJSON ou Shapefile)
  imazongeo-banco importar-sad dados/sad/*.geojson --modo real
  # SAD já importado no banco (tabelas alertas_sad_...) para o modelo
  imazongeo-banco importar-tabelas --modo real
  # Envio → banco → S3 (prévia por padrão; --modo simulation ou real)
  imazongeo-banco enviar simex_2025.zip --dataset simex --ano 2025 --modo real --public
  imazongeo-banco enviar ap_t3.zip --dataset ameaca_pressao --ano 2025 --trimestre 3
  # Histórico legado do S3 para o banco, e republicação no padrão novo
  imazongeo-banco baixar-legado
  imazongeo-banco importar-legado
  imazongeo-banco republicar --dataset simex --modo real --public

O banco vem de DATABASE_URL (.env).
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from ..config import DEFAULT_BUCKET, load_env, setup_logging
from ..s3 import usar_cliente_s3
from . import db, espelho, fluxo
from .padrao import PADROES, TODOS, Periodo


def _periodo(a: argparse.Namespace) -> Periodo:
    return Periodo(a.ano, a.trimestre)


def _args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="imazongeo-banco",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="comando", required=True)

    schema = sub.add_parser("criar-schema", help="Cria/atualiza as tabelas no banco")
    schema.add_argument(
        "--dataset", action="append", choices=list(TODOS), help="Padrão: todos"
    )

    sad = sub.add_parser(
        "importar-sad", help="Grava no banco os alertas do SAD (sem publicar no S3)"
    )
    sad.add_argument("arquivos", type=Path, nargs="+", help=".zip, .geojson ou .shp")
    sad.add_argument("--modo", choices=fluxo.MODOS, default="dry_run")

    tabelas = sub.add_parser(
        "importar-tabelas",
        help="Grava no modelo (sad_alerta) tabelas do SAD já existentes no banco",
    )
    tabelas.add_argument(
        "--tabela", action="append", help="Padrão: todas as alertas_sad_... do esquema"
    )
    tabelas.add_argument("--esquema", default="imazongeo")
    tabelas.add_argument("--modo", choices=fluxo.MODOS, default="dry_run")

    publicacao = argparse.ArgumentParser(add_help=False)
    publicacao.add_argument("--bucket", default=DEFAULT_BUCKET)
    publicacao.add_argument("--modo", choices=fluxo.MODOS, default="dry_run")
    publicacao.add_argument("--public", action="store_true", help="ACL public-read")

    enviar = sub.add_parser(
        "enviar", parents=[publicacao], help="Grava um envio no banco e publica no S3"
    )
    enviar.add_argument(
        "arquivo", type=Path, help=".zip (GeoJSON/Shapefile) ou .geojson"
    )
    enviar.add_argument("--dataset", choices=list(PADROES), required=True)
    enviar.add_argument("--ano", type=int, required=True)
    enviar.add_argument("--trimestre", type=int, choices=[1, 2, 3, 4])

    repub = sub.add_parser(
        "republicar", parents=[publicacao], help="Gera e publica a partir do banco"
    )
    repub.add_argument("--dataset", choices=list(PADROES), required=True)
    repub.add_argument("--ano", type=int, help="Padrão: todos os períodos gravados")
    repub.add_argument("--trimestre", type=int, choices=[1, 2, 3, 4])

    for nome, ajuda in (
        ("baixar-legado", "Baixa os arquivos legados do S3 para o espelho local"),
        ("importar-legado", "Grava no banco os arquivos do espelho local"),
    ):
        c = sub.add_parser(nome, help=ajuda)
        c.add_argument("--dataset", action="append", choices=list(PADROES))
        c.add_argument("--ano-inicial", type=int, default=1985)
        c.add_argument("--ano-final", type=int, default=date.today().year)
        c.add_argument("--destino", type=Path, default=espelho.DESTINO_PADRAO)
        if nome == "importar-legado":
            c.add_argument("--forcar", action="store_true")
    return p.parse_args(argv)


def _resumo_sad(resultado) -> None:
    for particao in sorted(resultado.registros):
        logging.info(
            "%s: %d alerta(s), %d mês(es)",
            particao,
            resultado.registros[particao],
            resultado.meses[particao],
        )


def _executar(a: argparse.Namespace) -> None:
    if a.comando == "criar-schema":
        with db.conectar() as conn:
            db.criar_schema(conn, a.dataset)
    elif a.comando == "importar-sad":
        from . import carga_sad

        if a.modo != "dry_run":
            with db.conectar() as conn:
                db.criar_schema(conn, ["sad"])
        _resumo_sad(carga_sad.gravar_caminhos(a.arquivos, a.modo))
    elif a.comando == "importar-tabelas":
        from . import carga_sad

        _resumo_sad(carga_sad.gravar_tabelas(a.tabela, a.modo, esquema=a.esquema))
    elif a.comando == "enviar":
        r = fluxo.processar_envio(
            a.arquivo,
            a.dataset,
            _periodo(a),
            bucket=a.bucket,
            modo=a.modo,
            public=a.public,
        )
        logging.info("Concluído (%s): %s", a.modo, r.envio.resumo())
    elif a.comando == "republicar":
        periodos = [_periodo(a)] if a.ano else None
        chaves = fluxo.republicar(
            a.dataset, periodos, bucket=a.bucket, modo=a.modo, public=a.public
        )
        logging.info("Concluído (%s): %d arquivo(s) publicados", a.modo, len(chaves))
    elif a.comando == "baixar-legado":
        datasets = a.dataset or list(PADROES)
        arquivos = espelho.descobrir(datasets, a.ano_inicial, a.ano_final)
        espelho.baixar_todos(arquivos, a.destino)
    elif a.comando == "importar-legado":
        datasets = a.dataset or list(PADROES)
        with db.conectar() as conn:
            db.criar_schema(conn, datasets)
        resumo = fluxo.importar_legado(
            a.destino, datasets, a.ano_inicial, a.ano_final, a.forcar
        )
        logging.info("Registros importados: %s", resumo or "nenhum")


def main(argv: Sequence[str] | None = None) -> int:
    load_env()
    a = _args(argv)
    setup_logging(a.verbose)
    try:
        if getattr(a, "modo", None) == "simulation":
            from ..simulador import SimuladorS3

            simulador = SimuladorS3()
            with usar_cliente_s3(simulador):
                _executar(a)
            logging.info("%s", simulador.relatorio())
        else:
            _executar(a)
    except (ValueError, RuntimeError) as e:
        logging.error("%s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
