"""Gera um executável Windows (.exe) da interface gráfica com PyInstaller.

Uso (na raiz do repositório, no Windows)::

    python -m pip install -e ".[build]"
    python scripts/windows/build_exe.py

O executável ficará em ``dist/ImazonUploadS3.exe``.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / ".env"
LAUNCHER = Path(__file__).resolve().parent / "imazon_gui.py"

# Hidden imports necessários para Windows com geopandas, boto3, etc.
HIDDEN_IMPORTS = [
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

COLLECT_ALL = [
    "imazongeo_upload",
    "geopandas",
    "fiona",
    "pyogrio",
    "pyproj",
    "shapely",
    "boto3",
    "botocore",
    "s3transfer",
]


def main() -> None:
    """Roda o PyInstaller com o .env da raiz empacotado no executável."""
    if not ENV_FILE.is_file():
        sys.exit(
            "Arquivo .env não encontrado na raiz do repositório.\n"
            "Crie um .env com ACCESS_KEY, PRIVATE_KEY, AWS_REGION e "
            "UPLOAD_PASSWORD\npreenchidos antes de gerar o executável — essas "
            "credenciais serão\nempacotadas dentro do .exe para carregamento "
            "automático."
        )

    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",  # tudo em um único arquivo
        "--windowed",  # sem janela de console (GUI pura)
        "--name",
        "ImazonUploadS3",
        "--paths",
        str(ROOT / "src"),
        # Empacota o .env dentro do .exe: as credenciais são carregadas
        # automaticamente ao abrir, em qualquer computador (ver gui.py)
        "--add-data",
        f"{ENV_FILE}{os.pathsep}.",
    ]
    for pacote in COLLECT_ALL:
        args += ["--collect-all", pacote]
    for imp in HIDDEN_IMPORTS:
        args += ["--hidden-import", imp]
    args.append(str(LAUNCHER))

    print("Rodando PyInstaller…")
    print(" ".join(args))
    subprocess.run(args, check=True, cwd=ROOT)
    print("\nPronto! Executável gerado em: dist/ImazonUploadS3.exe")
    print(
        "\nAs credenciais do .env usado no build foram empacotadas dentro do\n"
        ".exe e serão carregadas automaticamente em qualquer computador — não\n"
        "é necessário copiar um .env junto. Para trocar as credenciais depois,\n"
        "gere um novo .exe (ou coloque um .env ao lado do .exe para sobrescrever)."
    )


if __name__ == "__main__":
    main()
