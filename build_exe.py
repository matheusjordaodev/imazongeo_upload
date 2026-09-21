#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Gera um executável Windows (.exe) com PyInstaller.

Uso:
    pip install pyinstaller
    python build_exe.py

O executável ficará em:  dist/ImazonUploadS3.exe
"""

import subprocess
import sys
from pathlib import Path

_ENV_FILE = Path(__file__).parent / ".env"
if not _ENV_FILE.is_file():
    sys.exit(
        "Arquivo .env não encontrado ao lado de build_exe.py.\n"
        "Crie um .env com ACCESS_KEY, PRIVATE_KEY, AWS_REGION e UPLOAD_PASSWORD\n"
        "preenchidos antes de gerar o executável — essas credenciais serão\n"
        "empacotadas dentro do .exe para carregamento automático."
    )

# Hidden imports necessários para Windows com geopandas, boto3, etc.
hidden_imports = [
    # geopandas e dependências
    "geopandas",
    "geopandas.io.file",
    "pyogrio",
    "fiona",
    "fiona.ogrext",
    "shapely",
    "shapely.geometry",
    "pyproj",
    "pyproj.datadir",
    # pandas / numpy
    "pandas",
    "numpy",
    # dotenv
    "dotenv",
]

hidden_args = []
for imp in hidden_imports:
    hidden_args += ["--hidden-import", imp]

args = [
    sys.executable, "-m", "PyInstaller",
    "--onefile",          # tudo em um único arquivo
    "--windowed",         # sem janela de console (GUI pura)
    "--name", "ImazonUploadS3",
    # empacota o .env dentro do .exe: as credenciais são carregadas
    # automaticamente ao abrir, em qualquer computador (ver app.py)
    "--add-data", f"{_ENV_FILE};.",
    "--collect-all", "geopandas",
    "--collect-all", "fiona",
    "--collect-all", "pyogrio",
    "--collect-all", "pyproj",
    "--collect-all", "shapely",
    "--collect-all", "boto3",
    "--collect-all", "botocore",
    "--collect-all", "s3transfer",
    *hidden_args,
    "app.py",
]

print("Rodando PyInstaller para Windows…")
print(" ".join(args))
subprocess.run(args, check=True)
print("\nPronto! Executável gerado em: dist/ImazonUploadS3.exe")
print(
    "\nAs credenciais do .env usado no build foram empacotadas dentro do\n"
    ".exe e serão carregadas automaticamente em qualquer computador — não\n"
    "é necessário copiar um .env junto. Para trocar as credenciais depois,\n"
    "gere um novo .exe (ou coloque um .env ao lado do .exe para sobrescrever)."
)
