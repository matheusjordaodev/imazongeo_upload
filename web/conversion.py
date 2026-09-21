"""Conversão de ZIPs de GeoJSON para as bases não SAD."""
import json
import logging
from pathlib import Path, PurePosixPath
import shutil
import zipfile

import main as core


def extract_geojsons(archive_path, target):
    target.mkdir(parents=True, exist_ok=True)
    paths = []
    names = set()
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            name = PurePosixPath(member.filename.replace('\\', '/')).name
            suffix = Path(name).suffix.lower()
            if suffix in ('.shp', '.shx', '.dbf', '.csv'):
                raise ValueError('O ZIP deve conter GeoJSONs, não Shapefiles ou CSVs.')
            if suffix != '.geojson':
                continue
            if name.lower() in names:
                raise ValueError('Há nomes de GeoJSON repetidos no ZIP.')
            names.add(name.lower())
            path = target / name
            with archive.open(member) as source, path.open('wb') as output:
                shutil.copyfileobj(source, output)
            try:
                with path.open(encoding='utf-8-sig') as source:
                    data = json.load(source)
                if not isinstance(data, dict) or data.get('type') != 'FeatureCollection' or not isinstance(data.get('features'), list) or not data['features']:
                    raise ValueError()
            except (ValueError, UnicodeError) as exc:
                raise ValueError(f'{name}: informe uma FeatureCollection GeoJSON não vazia.') from exc
            paths.append(path)
    if not paths:
        raise ValueError('O ZIP deve conter pelo menos um arquivo .geojson.')
    return paths


def repair_geometries(frame, name):
    from shapely import make_valid
    if frame.empty or frame.geometry.isna().any() or frame.geometry.is_empty.any():
        raise ValueError(f'{name}: GeoJSON vazio ou com geometrias ausentes/vazias; não é possível reparar sem descartar registros.')
    invalid = ~frame.geometry.is_valid
    if invalid.any():
        frame = frame.copy()
        frame.loc[invalid, frame.geometry.name] = frame.loc[invalid].geometry.map(make_valid)
        if not frame.geometry.is_valid.all() or frame.geometry.is_empty.any():
            raise ValueError(f'{name}: algumas geometrias continuam inválidas após o reparo.')
        logging.warning('%s: %d geometria(s) reparada(s) automaticamente; %d registros preservados.', name, int(invalid.sum()), len(frame))
    unsupported = ~frame.geom_type.isin(['Polygon', 'MultiPolygon'])
    if unsupported.any():
        raise ValueError(f'{name}: {int(unsupported.sum())} geometria(s) não poligonais; conversão interrompida para não descartar componentes.')
    return frame


def ap_periods(frame):
    import pandas as pd
    columns = {str(c).upper(): c for c in frame.columns}
    year = pd.to_numeric(frame[columns['ANO']], errors='coerce') if 'ANO' in columns else None
    quarter = pd.to_numeric(frame[columns['TRIMESTRE']], errors='coerce') if 'TRIMESTRE' in columns else None
    if quarter is None and 'MES' in columns:
        month = pd.to_numeric(frame[columns['MES']], errors='coerce')
        if month.isna().any() or not (month.between(1, 12) & month.mod(1).eq(0)).all():
            raise ValueError('Ameaça & Pressão: campo MES inválido.')
        quarter = (month - 1) // 3 + 1
    for values, low, high in ((year, 1900, 2100), (quarter, 1, 4)):
        if values is not None and (values.isna().any() or not (values.between(low, high) & values.mod(1).eq(0)).all()):
            raise ValueError('Ameaça & Pressão: ANO/TRIMESTRE inválido nos atributos.')
    return year, quarter


def select_ap_period(frame, year, quarter):
    years, quarters = ap_periods(frame)
    selected = frame.copy()
    if years is not None:
        selected = selected.loc[years.eq(year)]
    if quarters is not None:
        selected = selected.loc[quarters.loc[selected.index].eq(quarter)]
    if selected.empty:
        raise ValueError(f'Ameaça & Pressão: nenhum registro para T{quarter}/{year}.')
    selected['ANO'] = year
    selected['TRIMESTRE'] = quarter
    logging.info('Ameaça & Pressão: T%d/%d selecionado, %d registros.', quarter, year, len(selected))
    return selected


def merge_ap_period(old, new, year, quarter):
    import geopandas as gpd
    import pandas as pd
    years, quarters = ap_periods(old)
    if years is None or quarters is None:
        raise ValueError('Histórico de Ameaça & Pressão sem ANO e TRIMESTRE/MES: não é possível substituir apenas o trimestre com segurança.')
    retained = old.loc[~(years.eq(year) & quarters.eq(quarter))].copy()
    retained['ANO'] = years.loc[retained.index].astype(int)
    retained['TRIMESTRE'] = quarters.loc[retained.index].astype(int)
    logging.info('Ameaça & Pressão: %d registros do trimestre substituídos; %d registros de outros períodos mantidos.', len(old) - len(retained), len(retained))
    return gpd.GeoDataFrame(pd.concat([retained.to_crs(new.crs), new], ignore_index=True), crs=new.crs)


def process_zip(options, base):
    import geopandas as gpd
    import pandas as pd
    import pyogrio

    o = options
    cfg = core.DATASETS[o['dataset']]
    category_mode = o['dataset'] == 'ameaca_pressao' and o['op'] == 'dashboard'
    frames = []
    for path in o['geojsons']:
        frame = repair_geometries(gpd.read_file(path), path.name)
        if frame.crs is None:
            frame = frame.set_crs(epsg=4326)
        frame = frame.to_crs(epsg=4326)
        if o['dataset'] == 'ameaca_pressao':
            frame = select_ap_period(frame, o['year'], o['quarter'])
        frames.append((path, frame))
    period = core.descobrir_periodo(cfg, o['year'], o['month'], o['quarter'])
    groups = [(core._nome_s3_ap(path.name)[:-8], frame) for path, frame in frames] if category_mode else [
        (Path(core.montar_nome_arquivo(cfg, period, 'geojson')).stem,
         gpd.GeoDataFrame(pd.concat([frame for _, frame in frames], ignore_index=True), crs='EPSG:4326'))]
    client = core.create_s3_client() if o['mode'] != 'dry_run' else None
    outputs = []
    for index, (stem, frame) in enumerate(groups):
        folder = base / 'converted' / str(index)
        folder.mkdir(parents=True, exist_ok=True)
        geojson = folder / f'{stem}.geojson'
        pyogrio.write_dataframe(frame, geojson, driver='GeoJSON')
        if o['op'] == 'dashboard' and client:
            old = folder / 'existing.geojson'
            key = core._s3_dashboard_prefix(cfg.s3_root, 'geojson') + geojson.name
            if core.baixar_arquivo_s3(client, o['bucket'], key, old):
                merged = folder / 'merged.geojson'
                if category_mode:
                    frame = merge_ap_period(gpd.read_file(old), frame, o['year'], o['quarter'])
                else:
                    core.concatenar_geojson(old, geojson, merged)
                    frame = gpd.read_file(merged)
                frame = repair_geometries(frame, geojson.name)
                pyogrio.write_dataframe(frame, geojson, driver='GeoJSON')
        elif o['op'] == 'dashboard':
            logging.info('[DRY RUN] Mesclaria o GeoJSON existente antes de gerar os três formatos.')
        csv = folder / f'{stem}.csv'
        frame.drop(columns=frame.geometry.name).to_csv(csv, index=False, encoding='utf-8')
        shp_dir = folder / 'shapefile'
        shp_dir.mkdir()
        # Shapefile não suporta atributos estruturados; serializa-os como texto.
        shape_frame = frame.copy()
        for column in shape_frame.columns:
            if column != shape_frame.geometry.name:
                shape_frame[column] = shape_frame[column].map(lambda value: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value)
        pyogrio.write_dataframe(shape_frame, shp_dir / f'{stem}.shp', driver='ESRI Shapefile', encoding='UTF-8')
        shp_zip = folder / f'{stem}.zip'
        core._zipar_diretorio(shp_dir, shp_zip)
        for fmt, path in [('geojson', geojson), ('csv', csv), ('shapefile', shp_zip)]:
            prefix = core._s3_dashboard_prefix(cfg.s3_root, fmt) if o['op'] == 'dashboard' else f'{cfg.s3_root}/{fmt}/'
            outputs.append((path, prefix))
    # Todas as conversões terminam antes do primeiro upload.
    core.enviar_arquivos_para_s3_multi(outputs, o['bucket'], dry_run=o['mode'] == 'dry_run', public=o['public'])
