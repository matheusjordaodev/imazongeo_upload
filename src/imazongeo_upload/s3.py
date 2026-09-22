"""Acesso ao AWS S3: criação do cliente, download e upload de arquivos."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import aws_region

try:
    import boto3  # necessário para upload real
except ImportError:  # pragma: no cover
    boto3 = None

# Cliente devolvido por create_s3_client() enquanto usar_cliente_s3() estiver
# ativo (ex.: o SimuladorS3 na simulação completa).
_cliente_substituto: Any = None


@contextmanager
def usar_cliente_s3(cliente: Any) -> Iterator[Any]:
    """Faz ``create_s3_client()`` devolver ``cliente`` dentro do bloco ``with``."""
    global _cliente_substituto
    anterior = _cliente_substituto
    _cliente_substituto = cliente
    try:
        yield cliente
    finally:
        _cliente_substituto = anterior


def create_s3_client() -> Any:
    """Cria o cliente S3 (ou devolve o substituto de ``usar_cliente_s3``).

    As credenciais vêm do ambiente/.env::

        ACCESS_KEY=
        PRIVATE_KEY=
        AWS_REGION=  (opcional, default us-east-1)
    """
    if _cliente_substituto is not None:
        return _cliente_substituto
    return _novo_cliente_boto3()


def _novo_cliente_boto3() -> Any:
    if boto3 is None:
        raise RuntimeError("boto3 não está instalado. Instale com 'pip install boto3'.")

    access_key = os.getenv("ACCESS_KEY")
    secret_key = os.getenv("PRIVATE_KEY")

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
        region_name=aws_region(),
    )


def baixar_arquivo_s3(s3_client: Any, bucket: str, key: str, dest_path: Path) -> bool:
    """Baixa um arquivo do S3 para ``dest_path``.

    Retorna True se o arquivo existia e foi baixado, False se não existia
    (404/NoSuchKey). Outros erros (permissão, rede) propagam normalmente.
    """
    try:
        s3_client.download_file(bucket, key, str(dest_path))
    except Exception as exc:
        # botocore.exceptions.ClientError: código 404 ou NoSuchKey
        resposta = getattr(exc, "response", None) or {}
        code = resposta.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey"):
            logging.info(
                "Arquivo não encontrado no S3 (será primeiro upload): s3://%s/%s",
                bucket,
                key,
            )
            return False
        raise
    logging.info("Baixado do S3: s3://%s/%s -> %s", bucket, key, dest_path)
    return True


def enviar_arquivo_s3(
    s3_client: Any, arquivo: Path, bucket: str, key: str, public: bool
) -> None:
    """Envia um arquivo para o S3 e, se ``public``, aplica ACL public-read."""
    logging.info("Enviando %s → s3://%s/%s …", arquivo.name, bucket, key)
    s3_client.upload_file(str(arquivo), bucket, key)
    logging.info("OK: s3://%s/%s", bucket, key)
    if public:
        s3_client.put_object_acl(Bucket=bucket, Key=key, ACL="public-read")
        logging.info("ACL public-read aplicada em s3://%s/%s", bucket, key)


def enviar_arquivos_para_s3_multi(
    arquivos: list[tuple[Path, str]],
    bucket: str,
    dry_run: bool = True,
    public: bool = False,
) -> None:
    """Envia (ou simula enviar) arquivos para o S3, cada um com seu prefixo.

    arquivos: lista de tuplas (Path, prefixo_s3).
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
                logging.info(
                    "[DRY RUN] Definiria ACL public-read em s3://%s/%s", bucket, key
                )
            continue

        logging.info("Enviando %s para s3://%s/%s ...", arq, bucket, key)
        s3_client.upload_file(str(arq), bucket, key)
        logging.info("OK: %s -> s3://%s/%s", arq.name, bucket, key)

        if public:
            logging.info("Definindo ACL public-read para s3://%s/%s ...", bucket, key)
            s3_client.put_object_acl(Bucket=bucket, Key=key, ACL="public-read")
            logging.info("ACL public-read aplicada em s3://%s/%s", bucket, key)
