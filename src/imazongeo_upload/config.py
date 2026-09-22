"""Configuração compartilhada: variáveis de ambiente (.env) e logging."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

# Raiz do repositório quando o pacote roda a partir do código-fonte (src/)
PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_BUCKET = "imazongeo3-web"
DEFAULT_REGION = "us-east-1"

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"


def load_env(env_file: str | os.PathLike[str] | None = None) -> Path | None:
    """Carrega as variáveis do primeiro arquivo .env encontrado.

    Ordem de busca: ``env_file``, a variável de ambiente ``IMAZON_ENV_FILE``,
    ``.env`` no diretório atual e ``.env`` na raiz do projeto. Variáveis já
    definidas no ambiente (ex.: pelo systemd) não são sobrescritas.

    Retorna o caminho do arquivo carregado, ou None se nenhum existir.
    """
    candidatos = (
        env_file,
        os.getenv("IMAZON_ENV_FILE"),
        Path.cwd() / ".env",
        PROJECT_ROOT / ".env",
    )
    for candidato in candidatos:
        if candidato and Path(candidato).is_file():
            load_dotenv(candidato)
            return Path(candidato)
    return None


def aws_region() -> str:
    """Região AWS configurada (AWS_REGION ou AWS_DEFAULT_REGION)."""
    return os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or DEFAULT_REGION


def setup_logging(verboso: bool = False) -> None:
    """Configura o logging do console (DEBUG se ``verboso``)."""
    logging.basicConfig(
        level=logging.DEBUG if verboso else logging.INFO,
        format=LOG_FORMAT,
        datefmt="%Y-%m-%d %H:%M:%S",
    )
