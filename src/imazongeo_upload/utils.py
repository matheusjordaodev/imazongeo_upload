"""Funções auxiliares de arquivos."""

from __future__ import annotations

import zipfile
from pathlib import Path


def zipar_diretorio(dir_origem: Path, output_zip: Path) -> None:
    """Compacta todos os arquivos (não recursivo) de dir_origem em output_zip."""
    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zout:
        for parte in sorted(dir_origem.iterdir()):
            if parte.is_file():
                zout.write(parte, parte.name)
