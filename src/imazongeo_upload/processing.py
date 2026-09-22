"""Fluxo genérico de Floreser, Ameaça & Pressão e SIMEX (download/dashboard).

O SAD tem fluxo próprio (:mod:`imazongeo_upload.sad`) e os dashboards de
A&P e SIMEX também (:mod:`imazongeo_upload.ameaca_pressao` e
:mod:`imazongeo_upload.simex`).
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from .concat import concatenar_com_s3
from .datasets import (
    DATASETS,
    FORMATS,
    DatasetConfig,
    descobrir_periodo,
    montar_diretorio_base_local,
    montar_nome_arquivo,
    prefixo_dashboard,
)
from .s3 import create_s3_client, enviar_arquivos_para_s3_multi

_EXT_TO_FORMAT = {".zip": "shapefile", ".csv": "csv", ".geojson": "geojson"}

# Extensões aceitas para o arquivo escolhido pelo usuário, por formato
_EXTENSOES_ARQUIVO = {
    "shapefile": (".zip",),
    "csv": (".csv",),
    "geojson": (".geojson", ".json"),
}


def coletar_arquivos_multi_tipo(
    base_dir: Path,
    cfg: DatasetConfig,
    periodo: dict,
    formats: list[str] | None = None,
    op: str = "download",
) -> list[tuple[Path, str]]:
    """Coleta os arquivos esperados de cada formato que existirem na pasta base.

    Retorna ``[(caminho_arquivo, s3_prefix), ...]``, com:

    - op="download"  → s3_prefix = '{s3_root}/{fmt}/'
    - op="dashboard" → s3_prefix = prefixo_dashboard(cfg, fmt)

    formats: subconjunto de FORMATS a processar; None = todos.
    """
    base_local = montar_diretorio_base_local(base_dir, cfg, periodo)

    arquivos: list[tuple[Path, str]] = []

    if not base_local.exists():
        logging.warning("Pasta base local não encontrada: %s", base_local)

    for tipo in formats or FORMATS:
        nome_arquivo = montar_nome_arquivo(cfg, periodo, tipo)
        caminho_arquivo = base_local / tipo / nome_arquivo

        if not caminho_arquivo.exists():
            logging.warning(
                "Arquivo %s tipo '%s' não encontrado (esperado: %s)",
                cfg.slug,
                tipo,
                caminho_arquivo,
            )
            continue

        if op == "dashboard":
            s3_prefix = prefixo_dashboard(cfg, tipo)
        else:
            s3_prefix = f"{cfg.s3_root}/{tipo}/"
        arquivos.append((caminho_arquivo, s3_prefix))
        logging.info("Arquivo %s (%s) encontrado: %s", cfg.slug, tipo, caminho_arquivo)

    return arquivos


def _arquivo_com_nome_padrao(
    arquivo: Path,
    cfg: DatasetConfig,
    periodo: dict,
    formats: list[str] | None,
    op: str,
    tmp: Path,
) -> tuple[Path, str]:
    """Copia o arquivo escolhido para ``tmp`` com o nome padrão do período.

    O nome padrão (ex.: floreser_2025.geojson) é o nome usado no S3.
    Retorna (caminho, prefixo_s3).
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
        return destino, prefixo_dashboard(cfg, fmt)
    return destino, f"{cfg.s3_root}/{fmt}/"


def processar_dataset(
    nome_dataset: str,
    base_dir: Path,
    bucket: str,
    year: int | None,
    month: int | None,
    quarter: int | None,
    dry_run: bool,
    public: bool,
    formats: list[str] | None = None,
    concatenar: bool = False,
    op: str = "download",
    arquivo: Path | None = None,
) -> None:
    """Processa Floreser, Ameaça & Pressão e SIMEX.

    arquivo: arquivo escolhido pelo usuário (um só formato em ``formats``);
             é enviado com o nome padrão do período. Se omitido, os arquivos
             são procurados em {base_dir}/{dataset}/...

    op="download"  → upload direto ao prefixo {s3_root}/{fmt}/
    op="dashboard" → concatena com o S3 e envia ao prefixo de dashboard,
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
        logging.warning(
            "O dashboard de %s tem fluxo próprio: use processar_%s_dashboard.",
            cfg.descricao,
            cfg.slug,
        )
        return

    periodo = descobrir_periodo(cfg, year, month, quarter)
    logging.info("Período detectado: %s", periodo)

    if arquivo is not None:
        with tempfile.TemporaryDirectory(prefix="imazon_arquivo_") as tmp:
            arquivos = [
                _arquivo_com_nome_padrao(
                    Path(arquivo), cfg, periodo, formats, op, Path(tmp)
                )
            ]
            _enviar_arquivos_dataset(
                arquivos, cfg, base_dir, bucket, dry_run, public, concatenar, op
            )
        return

    arquivos = coletar_arquivos_multi_tipo(
        base_dir, cfg, periodo, formats=formats, op=op
    )

    if not arquivos:
        logging.warning(
            "Nenhum arquivo encontrado para %s no período %s.", cfg.slug, periodo
        )
        return

    _enviar_arquivos_dataset(
        arquivos, cfg, base_dir, bucket, dry_run, public, concatenar, op
    )


def _enviar_arquivos_dataset(
    arquivos: list[tuple[Path, str]],
    cfg: DatasetConfig,
    base_dir: Path,
    bucket: str,
    dry_run: bool,
    public: bool,
    concatenar: bool,
    op: str,
) -> None:
    """Envia os arquivos de um dataset.

    No dashboard (ou com ``concatenar``), junta antes com a versão do S3.
    """
    if not (concatenar or op == "dashboard"):
        enviar_arquivos_para_s3_multi(
            arquivos, bucket=bucket, dry_run=dry_run, public=public
        )
        return

    if dry_run:
        for arq, prefixo_s3 in arquivos:
            key = prefixo_s3 + arq.name
            logging.info(
                "[DRY RUN] Baixaria s3://%s/%s, concatenaria com %s e reenviaria",
                bucket,
                key,
                arq,
            )
        return

    s3_client = create_s3_client()
    with tempfile.TemporaryDirectory() as tmp:
        arquivos_merged = concatenar_com_s3(arquivos, bucket, s3_client, Path(tmp))

        # Para dashboard, persiste cópia local em {base_dir}/dashboard/{s3_root}/{fmt}/
        if op == "dashboard":
            for merged_path, _ in arquivos_merged:
                fmt_key = _EXT_TO_FORMAT.get(
                    merged_path.suffix.lower(), merged_path.suffix.lstrip(".")
                )
                local_dir = base_dir / "dashboard" / cfg.s3_root / fmt_key
                local_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(merged_path, local_dir / merged_path.name)
                logging.info("Salvo localmente: %s", local_dir / merged_path.name)

        enviar_arquivos_para_s3_multi(
            arquivos_merged, bucket=bucket, dry_run=False, public=public
        )
