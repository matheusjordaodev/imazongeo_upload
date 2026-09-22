"""Simulação de envio para o AWS S3, sem credenciais.

Fornece :class:`SimuladorS3`, um cliente falso que implementa a interface
mínima do cliente S3 do boto3 usada pelo pacote::

    upload_file(filename, bucket, key, **kwargs)
    download_file(bucket, key, filename)
    put_object_acl(Bucket, Key, ACL)

Os arquivos "enviados" ficam num diretório local (temporário ou fixo),
permitindo testar todo o fluxo — descoberta de arquivos, concatenação e
upload — sem acesso à AWS.

Uso direto (CLI)::

    imazongeo-simulador sad --bucket imazongeo3-web --zip shapefile.zip

Executa o mesmo fluxo de ``imazongeo-upload``, mas com o SimuladorS3 como
backend, e imprime um relatório do que teria sido enviado.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import tempfile
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from .ameaca_pressao import processar_ameaca_pressao_dashboard
from .config import DEFAULT_BUCKET, setup_logging
from .datasets import DATASETS
from .processing import processar_dataset
from .s3 import usar_cliente_s3
from .sad import processar_sad_zip
from .simex import processar_simex_dashboard

# ---------------------------------------------------------------------------
# Cliente S3 simulado
# ---------------------------------------------------------------------------


class SimuladorS3:
    """Substituto do cliente S3 do boto3 para fins de simulação.

    Os arquivos "enviados" são copiados para ``storage_dir``, organizados
    como ``<storage_dir>/<bucket>/<key>``. Os "baixados" são lidos do mesmo
    diretório; se não existirem, é lançado um erro compatível com o
    esperado por :func:`imazongeo_upload.s3.baixar_arquivo_s3` (404/NoSuchKey).
    """

    def __init__(self, storage_dir: Path | None = None) -> None:
        if storage_dir is None:
            # Diretório temporário gerenciado pelo próprio simulador
            self._tmp: str | None = tempfile.mkdtemp(prefix="imazon_sim_s3_")
            self.storage_dir = Path(self._tmp)
        else:
            storage_dir.mkdir(parents=True, exist_ok=True)
            self._tmp = None
            self.storage_dir = storage_dir

        # Registro de operações realizadas
        self.uploads: list[dict] = []
        self.downloads: list[dict] = []
        self.acl_ops: list[dict] = []

    # ------------------------------------------------------------------
    # Interface do cliente S3 do boto3
    # ------------------------------------------------------------------

    def upload_file(self, filename: str, bucket: str, key: str, **kwargs: Any) -> None:
        """Copia ``filename`` para o armazenamento simulado."""
        src = Path(filename)
        dest = self.storage_dir / bucket / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

        size_kb = src.stat().st_size / 1024
        logging.info(
            "[SIM] Upload: %s -> s3://%s/%s (%.1f KB)", src.name, bucket, key, size_kb
        )
        self.uploads.append(
            {"file": str(src), "bucket": bucket, "key": key, "size_kb": size_kb}
        )

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        """Copia o objeto simulado para ``filename`` (erro NoSuchKey se faltar)."""
        src = self.storage_dir / bucket / key
        if not src.exists():
            raise _FakeClientError(bucket, key)

        shutil.copy2(src, filename)
        logging.info("[SIM] Download: s3://%s/%s -> %s", bucket, key, filename)
        self.downloads.append({"bucket": bucket, "key": key, "dest": filename})

    def put_object_acl(self, Bucket: str, Key: str, ACL: str) -> None:  # noqa: N803
        """Registra a ACL (os nomes dos parâmetros seguem o boto3)."""
        logging.info("[SIM] ACL %s aplicada em s3://%s/%s", ACL, Bucket, Key)
        self.acl_ops.append({"bucket": Bucket, "key": Key, "acl": ACL})

    # ------------------------------------------------------------------
    # Relatório
    # ------------------------------------------------------------------

    def relatorio(self) -> str:
        """Resumo textual dos uploads, downloads e ACLs simulados."""
        linhas = [
            "=" * 60,
            "RELATÓRIO DE SIMULAÇÃO S3",
            "=" * 60,
        ]

        if self.uploads:
            linhas.append(f"\nArquivos enviados ({len(self.uploads)}):")
            for u in self.uploads:
                linhas.append(
                    f"  s3://{u['bucket']}/{u['key']}  ({u['size_kb']:.1f} KB)"
                )
        else:
            linhas.append("\nNenhum arquivo enviado.")

        if self.downloads:
            linhas.append(
                f"\nArquivos baixados do S3 simulado ({len(self.downloads)}):"
            )
            for d in self.downloads:
                linhas.append(f"  s3://{d['bucket']}/{d['key']}")

        if self.acl_ops:
            linhas.append(f"\nOperações de ACL ({len(self.acl_ops)}):")
            for a in self.acl_ops:
                linhas.append(f"  {a['acl']} em s3://{a['bucket']}/{a['key']}")

        linhas.append("\n" + "=" * 60)
        return "\n".join(linhas)

    def limpar(self) -> None:
        """Remove o diretório temporário criado pelo simulador (se aplicável)."""
        if self._tmp and Path(self._tmp).exists():
            shutil.rmtree(self._tmp)

    def __del__(self) -> None:
        # Limpeza automática apenas do diretório interno gerenciado
        try:
            self.limpar()
        except Exception:
            pass


class _FakeClientError(Exception):
    """Simula botocore.exceptions.ClientError com .response['Error']['Code']."""

    def __init__(self, bucket: str, key: str) -> None:
        self.response = {
            "Error": {
                "Code": "NoSuchKey",
                "Message": f"s3://{bucket}/{key} não existe no simulador",
            }
        }
        super().__init__(str(self.response))


# ---------------------------------------------------------------------------
# Função de entrada principal para simulação
# ---------------------------------------------------------------------------


def executar_simulacao(
    dataset: str,
    bucket: str,
    base_dir: Path,
    year: int | None = None,
    month: int | None = None,
    quarter: int | None = None,
    public: bool = True,
    formats: list[str] | None = None,
    concatenar: bool = False,
    storage_dir: Path | None = None,
    op_download: bool = True,
    op_dashboard: bool = False,
    sad_zips: list[Path] | None = None,
    sad_todos_meses: bool = False,
    sad_dashboard_todos_meses: bool = True,
    arquivo: Path | None = None,
) -> SimuladorS3:
    """Executa o fluxo completo usando o SimuladorS3 como backend.

    Retorna o simulador após a execução, para que o chamador possa inspecionar
    o relatório e os arquivos "enviados".

    Parâmetros
    ----------
    dataset:      slug do dataset ('sad', 'floreser', 'ameaca_pressao',
                  'simex', 'all')
    bucket:       nome do bucket S3 de destino
    base_dir:     diretório base local com os dados
    year/month/quarter: filtros de período (None = data atual)
    public:       simular ACL public-read nos uploads
    formats:      subconjunto de ['shapefile', 'csv', 'geojson']; None = todos
    concatenar:   (não usado; mantido por compatibilidade)
    storage_dir:  diretório onde o simulador armazena os "uploads"; None = temp
    op_download:  simular operação de arquivos de download
    op_dashboard: simular operação de arquivos de dashboard
    sad_zips:     ZIP(s) recebido(s) do SAD (ver sad.processar_sad_zip)
    sad_todos_meses: gerar os arquivos mensais de todos os meses do ZIP
    sad_dashboard_todos_meses: CSVs do dashboard com todos os dados do ZIP
                  (False = só o mês)
    arquivo:      arquivo escolhido para Floreser / Ameaça & Pressão / SIMEX
                  (um dataset só)
    """
    sim = SimuladorS3(storage_dir=storage_dir)

    with usar_cliente_s3(sim):
        nomes = list(DATASETS) if dataset == "all" else [dataset]

        # SAD: dashboard e download saem juntos do ZIP, independente das operações
        if "sad" in nomes:
            nomes.remove("sad")
            if sad_zips:
                logging.info(">>> [SIM] SAD: dashboard + arquivos mensais")
                processar_sad_zip(
                    zips=sad_zips,
                    bucket=bucket,
                    dry_run=False,  # usa o simulador, não dry_run
                    ano=year,
                    mes=month,
                    todos_meses=sad_todos_meses,
                    dashboard_todos_meses=sad_dashboard_todos_meses,
                    public=public,
                )
            else:
                logging.warning("[SIM] SAD ignorado: nenhum ZIP informado.")

        if arquivo is not None and len(nomes) > 1:
            raise ValueError(
                "Um arquivo escolhido só pode ser simulado para um dataset por vez."
            )

        if op_download and nomes:
            logging.info(">>> [SIM] Operação: arquivos de download")
            for nome in nomes:
                processar_dataset(
                    nome_dataset=nome,
                    base_dir=base_dir,
                    bucket=bucket,
                    year=year,
                    month=month,
                    quarter=quarter,
                    dry_run=False,  # usa o simulador, não dry_run
                    public=public,
                    formats=formats,
                    concatenar=False,
                    op="download",
                    arquivo=arquivo,
                )

        if op_dashboard and nomes:
            logging.info(">>> [SIM] Operação: arquivos de dashboard")
            for nome in nomes:
                _simular_dashboard(
                    nome,
                    bucket,
                    base_dir,
                    year,
                    month,
                    quarter,
                    public,
                    formats,
                    arquivo,
                )

    return sim


def _simular_dashboard(
    nome: str,
    bucket: str,
    base_dir: Path,
    year: int | None,
    month: int | None,
    quarter: int | None,
    public: bool,
    formats: list[str] | None,
    arquivo: Path | None,
) -> None:
    # Mesmo fluxo da interface: A&P e SIMEX têm dashboard próprio
    if nome == "ameaca_pressao":
        processar_ameaca_pressao_dashboard(
            base_dir=base_dir,
            bucket=bucket,
            year=year or date.today().year,
            dry_run=False,
            public=public,
            arquivo=arquivo,
        )
    elif nome == "simex":
        if arquivo is None:
            logging.warning(
                "[SIM] Dashboard do SIMEX ignorado: nenhum arquivo informado."
            )
        else:
            processar_simex_dashboard(
                bucket=bucket, dry_run=False, public=public, arquivo=arquivo
            )
    else:
        processar_dataset(
            nome_dataset=nome,
            base_dir=base_dir,
            bucket=bucket,
            year=year,
            month=month,
            quarter=quarter,
            dry_run=False,
            public=public,
            formats=formats,
            concatenar=True,
            op="dashboard",
            arquivo=arquivo,
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="imazongeo-simulador",
        description="Simulação de upload de datasets Imazon para S3 "
        "(sem credenciais AWS).",
    )
    parser.add_argument(
        "dataset", choices=[*DATASETS, "all"], help="Dataset a simular."
    )
    parser.add_argument("--bucket", default=DEFAULT_BUCKET, help="Nome do bucket S3.")
    parser.add_argument(
        "--base-dir", default="dados", help="Diretório base dos dados locais."
    )
    parser.add_argument("--year", type=int, help="Ano de referência.")
    parser.add_argument("--month", type=int, choices=range(1, 13), help="Mês (SAD).")
    parser.add_argument(
        "--zip",
        dest="sad_zips",
        action="append",
        type=Path,
        help="SAD: ZIP recebido (repita para cada parte, se veio dividido).",
    )
    parser.add_argument(
        "--todos-meses",
        action="store_true",
        help="SAD: arquivos mensais de todos os meses do ZIP.",
    )
    parser.add_argument(
        "--dashboard-so-mes",
        action="store_true",
        help="SAD: CSVs do dashboard só com o mês, em vez de todos os dados do ZIP.",
    )
    parser.add_argument(
        "--quarter",
        type=int,
        choices=range(1, 5),
        help="Trimestre (Ameaça & Pressão).",
    )
    parser.add_argument(
        "--public", action="store_true", help="Simular ACL public-read."
    )
    parser.add_argument(
        "--concatenar",
        action="store_true",
        help="Simular fluxo de concatenação com S3.",
    )
    parser.add_argument(
        "--op-download",
        action="store_true",
        default=True,
        help="Simular arquivos de download.",
    )
    parser.add_argument(
        "--op-dashboard", action="store_true", help="Simular arquivos de dashboard."
    )
    parser.add_argument(
        "--storage-dir",
        help="Diretório para armazenar os uploads simulados (default: diretório "
        "temporário).",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Logs em DEBUG.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """Ponto de entrada do comando ``imazongeo-simulador``."""
    args = _parse_args(argv)
    setup_logging(args.verbose)

    storage = Path(args.storage_dir) if args.storage_dir else None
    base_dir = Path(args.base_dir).resolve()

    logging.info("=== Iniciando simulação ===")
    logging.info(
        "Dataset: %s | Bucket: %s | Base dir: %s", args.dataset, args.bucket, base_dir
    )

    sim = executar_simulacao(
        dataset=args.dataset,
        bucket=args.bucket,
        base_dir=base_dir,
        year=args.year,
        month=args.month,
        quarter=args.quarter,
        public=args.public,
        concatenar=args.concatenar,
        storage_dir=storage,
        op_download=args.op_download,
        op_dashboard=args.op_dashboard,
        sad_zips=args.sad_zips,
        sad_todos_meses=args.todos_meses,
        sad_dashboard_todos_meses=not args.dashboard_so_mes,
    )

    print(sim.relatorio())

    if storage:
        logging.info("Arquivos simulados disponíveis em: %s", storage)


if __name__ == "__main__":
    main()
