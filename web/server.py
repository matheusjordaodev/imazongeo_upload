"""Interface web local para o processamento Imazon existente."""
import hmac
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import sys
import tempfile
import threading
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / '.env')
import main as core
from simulador import SimuladorS3
from flask import Flask, jsonify, render_template, request, session
from conversion import extract_geojsons, process_zip

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
app.config.update(MAX_CONTENT_LENGTH=1024 * 1024 * 1024, SESSION_COOKIE_SAMESITE='Strict', SESSION_COOKIE_HTTPONLY=True)
lock = threading.Lock()
jobs = {}


@app.before_request
def protect():
    if request.host not in ('127.0.0.1:5000', 'localhost:5000'):
        return jsonify(error='Host não permitido.'), 403
    if request.method == 'POST' and not hmac.compare_digest(
        request.headers.get('X-CSRF-Token', ''), session.get('token', secrets.token_hex(32))
    ):
        return jsonify(error='Recarregue a página para iniciar uma sessão.'), 403


@app.get('/')
def index():
    session.setdefault('token', secrets.token_hex(32))
    return render_template('index.html', token=session['token'])


def validate_zip(path):
    with zipfile.ZipFile(path) as archive:
        for item in archive.infolist():
            name = PurePosixPath(item.filename.replace('\\', '/'))
            if name.is_absolute() or '..' in name.parts or ':' in item.filename:
                raise ValueError('ZIP contém um caminho inválido.')


def validate_sad_geojson(path):
    """Valida o conteúdo antes de permitir qualquer operação no S3."""
    try:
        with path.open(encoding='utf-8-sig') as source:
            data = json.load(source)
    except (ValueError, UnicodeError) as exc:
        raise ValueError('O arquivo SAD deve conter um GeoJSON válido.') from exc
    if not isinstance(data, dict) or data.get('type') != 'FeatureCollection':
        raise ValueError('O GeoJSON SAD deve ser uma FeatureCollection.')
    features = data.get('features')
    if not isinstance(features, list) or not features:
        raise ValueError('O GeoJSON SAD deve conter feições.')
    periods = set()
    from shapely.geometry import shape
    for feature in features:
        try:
            if feature.get('type') != 'Feature':
                raise ValueError()
            props = feature['properties']
            year, month = float(props['ANO']), float(props['MES'])
            if not year.is_integer() or not month.is_integer() or not 1900 <= year <= 2100 or not 1 <= month <= 12:
                raise ValueError()
            geometry = shape(feature['geometry'])
            if geometry.geom_type not in ('Polygon', 'MultiPolygon') or geometry.is_empty or not geometry.is_valid:
                raise ValueError()
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ValueError('Cada feição SAD deve ter ANO/MES válidos e geometria Polygon ou MultiPolygon válida.') from exc
        periods.add((int(year), int(month)))
    return periods


def prepare(form, uploads, base):
    dataset = form.get('dataset')
    mode = form.get('mode', 'real')
    op = form.get('operation', 'download')
    fmt = form.get('format', 'geojson')
    if dataset not in core.DATASETS or mode not in ('dry_run', 'simulation', 'real'):
        raise ValueError('Base ou modo inválido.')
    if op not in ('download', 'dashboard') or fmt not in core.FORMATS:
        raise ValueError('Operação ou formato inválido.')
    year, month, quarter = (int(form.get(k, '0')) for k in ('year', 'month', 'quarter'))
    if not 1900 <= year <= 2100 or not 1 <= month <= 12 or not 1 <= quarter <= 4:
        raise ValueError('Período inválido.')
    bucket = form.get('bucket', '').strip()
    if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', bucket):
        raise ValueError('Nome de bucket inválido.')
    if mode == 'real':
        password = os.getenv('UPLOAD_PASSWORD', '')
        if not password or not hmac.compare_digest(password, form.get('password', '')):
            raise ValueError('Senha de upload inválida ou UPLOAD_PASSWORD não configurada.')
        if form.get('confirm') != 'on':
            raise ValueError('Confirme a atualização dos objetos no S3.')
        if not os.getenv('ACCESS_KEY') or not os.getenv('PRIVATE_KEY'):
            raise ValueError('Configure ACCESS_KEY e PRIVATE_KEY no .env da aplicação.')
    uploads = [f for f in uploads if f.filename]
    if not uploads or (dataset != 'sad' and len(uploads) != 1):
        raise ValueError('Selecione um ZIP contendo GeoJSONs (o SAD aceita várias partes).')
    paths = []
    sad_layers = set()
    sad_periods = set()
    sad_normalized = []
    annual_zip = dataset == 'ameaca_pressao' and op == 'dashboard'
    for i, upload in enumerate(uploads):
        ext = Path(upload.filename).suffix.lower()
        expected = '.zip'
        if ext != expected:
            raise ValueError(f'Selecione arquivo {expected} para esta operação.')
        if dataset == 'sad':
            dest = base / f'parte_{i}.zip'
        elif annual_zip:
            dest = base / dataset / str(year) / f'ameaca_e_pressao_{year}.zip'
        else:
            dest = base / 'entrada.zip'
        dest.parent.mkdir(parents=True, exist_ok=True)
        upload.save(dest)
        if ext == '.zip':
            validate_zip(dest)
        if dataset == 'sad':
            with zipfile.ZipFile(dest) as archive:
                members = [m for m in archive.infolist() if not m.is_dir()]
                if any(Path(m.filename).suffix.lower() in ('.shp', '.shx', '.dbf', '.csv') for m in members):
                    raise ValueError('O ZIP do SAD deve conter GeoJSONs, não Shapefiles ou CSVs.')
                geojsons = [m for m in members if Path(m.filename).suffix.lower() == '.geojson']
                if not geojsons:
                    raise ValueError('O ZIP do SAD deve conter pelo menos um arquivo .geojson.')
                for member in geojsons:
                    name = PurePosixPath(member.filename.replace('\\', '/')).name
                    info = core._identificar_camada_sad(name)
                    if info is None:
                        tipo = form.get('sad_type', '')
                        camada = form.get('sad_layer', '')
                        if tipo not in core._SAD_CSV_DASHBOARD or camada not in core._SAD_CSV_DASHBOARD[tipo]:
                            raise ValueError('Selecione o tipo de alerta e a camada SAD para os arquivos com nome livre.')
                        info = (tipo, camada)
                    layer = info[:2]
                    if layer in sad_layers:
                        raise ValueError('Envie apenas um GeoJSON por tipo e camada SAD.')
                    sad_layers.add(layer)
                    extracted = base / 'validacao' / name
                    extracted.parent.mkdir(exist_ok=True)
                    with archive.open(member) as source, extracted.open('wb') as target:
                        shutil.copyfileobj(source, target)
                    periods = validate_sad_geojson(extracted)
                    sad_periods.update(periods)
                    start, end = min(periods), max(periods)
                    canonical = f'alertas_sad_{layer[0]}_{start[1]:02d}_{start[0]}_{end[1]:02d}_{end[0]}_{layer[1]}.geojson'
                    normalized = base / f'sad_normalizado_{len(sad_normalized)}.zip'
                    with zipfile.ZipFile(normalized, 'w', zipfile.ZIP_DEFLATED) as output:
                        output.write(extracted, canonical)
                    sad_normalized.append(normalized)
                    extracted.unlink()
        paths.append(dest)
    if dataset == 'sad':
        paths = sad_normalized
        if form.get('all_months') != 'on' and (year, month) not in sad_periods:
            raise ValueError('O GeoJSON não contém alertas para o mês e ano selecionados.')
    geojsons = extract_geojsons(paths[0], base / 'entrada') if dataset != 'sad' else []
    if annual_zip and any(not core._nome_s3_ap(p.name) for p in geojsons):
        raise ValueError('Todos os GeoJSONs de Ameaça & Pressão devem ter nomes de categoria reconhecidos.')
    return dict(dataset=dataset, mode=mode, op=op, fmt=fmt, year=year, month=month,
                quarter=quarter, bucket=bucket, paths=paths, geojsons=geojsons, all_months=form.get('all_months') == 'on',
                dashboard_all=form.get('dashboard_all') == 'on', public=form.get('public') == 'on')


class JobLog(logging.Handler):
    def __init__(self, job):
        super().__init__()
        self.job = job
        self.worker = threading.get_ident()
        self.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', '%H:%M:%S'))

    def emit(self, record):
        if record.thread == self.worker:
            self.job['logs'].append(self.format(record))
            self.job['logs'] = self.job['logs'][-3000:]


def execute(job, options, temp):
    handler = JobLog(job)
    logger = logging.getLogger()
    level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    original = core.create_s3_client
    simulator = None
    try:
        o = options
        base = Path(temp.name)
        if o['mode'] == 'simulation':
            simulator = SimuladorS3(base / '_s3')
            core.create_s3_client = lambda: simulator
        dry = o['mode'] == 'dry_run'
        if o['dataset'] == 'sad':
            core.processar_sad_zip(zips=o['paths'], bucket=o['bucket'], dry_run=dry,
                ano=o['year'], mes=o['month'], todos_meses=o['all_months'],
                dashboard_todos_meses=o['dashboard_all'], public=o['public'])
        else:
            process_zip(o, base)
        if simulator:
            logging.info(simulator.relatorio())
        logging.info('Processamento concluído. Modo: %s.', o['mode'])
        job['status'] = 'done'
    except Exception as exc:
        logging.error('%s', exc)
        job['status'] = 'error'
    finally:
        core.create_s3_client = original
        logger.removeHandler(handler)
        logger.setLevel(level)
        temp.cleanup()
        lock.release()


@app.post('/api/jobs')
def start_job():
    if not lock.acquire(blocking=False):
        return jsonify(error='Já existe um processamento em andamento.'), 409
    temp = tempfile.TemporaryDirectory(prefix='imazon_web_')
    try:
        options = prepare(request.form, request.files.getlist('files'), Path(temp.name))
        job_id = secrets.token_hex(16)
        job = dict(status='running', logs=[], owner=session['token'])
        while len(jobs) >= 20:
            jobs.pop(next(iter(jobs)))
        jobs[job_id] = job
        threading.Thread(target=execute, args=(job, options, temp), daemon=True).start()
        return jsonify(id=job_id), 202
    except (ValueError, zipfile.BadZipFile) as exc:
        temp.cleanup()
        lock.release()
        return jsonify(error=str(exc)), 400
    except Exception:
        temp.cleanup()
        lock.release()
        raise


@app.get('/api/jobs/<job_id>')
def job_status(job_id):
    job = jobs.get(job_id)
    if not job or job['owner'] != session.get('token'):
        return jsonify(error='Processamento não encontrado.'), 404
    return jsonify(status=job['status'], logs=job['logs'])


@app.errorhandler(413)
def too_large(error):
    return jsonify(error='O total de arquivos excede o limite de 1 GB.'), 413


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=False)
