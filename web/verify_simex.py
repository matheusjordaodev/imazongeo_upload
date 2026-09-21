"""Verifica conversão real com S3 exclusivamente local."""
import logging
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
import geopandas as gpd
import pandas as pd
import server
from conversion import extract_geojsons, process_zip

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
with tempfile.TemporaryDirectory(prefix='simex_check_') as directory:
    base = Path(directory)
    source = Path(sys.argv[1])
    server.validate_zip(source)
    paths = extract_geojsons(source, base / 'input')
    sim = server.SimuladorS3(base / 's3')
    options = dict(dataset='simex', op='download', year=2024, month=1, quarter=1,
                   mode='simulation', bucket='validation-local', public=False, geojsons=paths)
    with patch.object(server.core, 'create_s3_client', return_value=sim):
        process_zip(options, base)
    outputs = base / 's3' / 'validation-local' / 'simex'
    geo = gpd.read_file(outputs / 'geojson/simex_unificado_2024.geojson')
    csv = pd.read_csv(outputs / 'csv/simex_unificado_2024.csv', low_memory=False)
    shp = gpd.read_file(outputs / 'shapefile/simex_unificado_2024.zip')
    assert len(geo) == len(csv) == len(shp) == 61049, (len(geo), len(csv), len(shp))
    assert geo.is_valid.all() and shp.is_valid.all()
    print(f'PASS: {len(geo)} records in GeoJSON, CSV and Shapefile; all geometries valid; {len(sim.uploads)} local uploads.')
