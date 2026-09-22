"""Verifica a conversão real do SIMEX com S3 exclusivamente local.

Uso::

    python scripts/verify_simex.py simex_2024.zip
"""

import logging
import sys
import tempfile
from pathlib import Path

import geopandas as gpd
import pandas as pd

from imazongeo_upload.s3 import usar_cliente_s3
from imazongeo_upload.simulador import SimuladorS3
from imazongeo_upload.web.conversion import extract_geojsons, process_zip
from imazongeo_upload.web.server import validate_zip

# Quantidade de registros esperada no arquivo SIMEX 2024 de referência
REGISTROS_ESPERADOS = 61049


def main(source: Path) -> None:
    """Converte ``source`` num S3 simulado e confere os três formatos gerados."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    with tempfile.TemporaryDirectory(prefix="simex_check_") as directory:
        base = Path(directory)
        validate_zip(source)
        paths = extract_geojsons(source, base / "input")
        sim = SimuladorS3(base / "s3")
        options = {
            "dataset": "simex",
            "op": "download",
            "year": 2024,
            "month": 1,
            "quarter": 1,
            "mode": "simulation",
            "bucket": "validation-local",
            "public": False,
            "geojsons": paths,
        }
        with usar_cliente_s3(sim):
            process_zip(options, base)
        outputs = base / "s3" / "validation-local" / "simex"
        geo = gpd.read_file(outputs / "geojson/simex_unificado_2024.geojson")
        csv = pd.read_csv(outputs / "csv/simex_unificado_2024.csv", low_memory=False)
        shp = gpd.read_file(outputs / "shapefile/simex_unificado_2024.zip")
        contagens = (len(geo), len(csv), len(shp))
        assert contagens == (REGISTROS_ESPERADOS,) * 3, contagens
        assert geo.is_valid.all() and shp.is_valid.all()
        print(
            f"PASS: {len(geo)} records in GeoJSON, CSV and Shapefile; "
            f"all geometries valid; {len(sim.uploads)} local uploads."
        )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
