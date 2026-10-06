"""Interface web para o processamento dos datasets.

Desenvolvimento::

    python -m imazongeo_upload.web

Produção (Ubuntu), atrás do gunicorn — use um único worker, pois os
processamentos ficam em memória::

    gunicorn -c deploy/gunicorn.conf.py imazongeo_upload.web.server:app

Variáveis de ambiente (além das credenciais AWS e de UPLOAD_PASSWORD):

- ``WEB_HOST`` / ``WEB_PORT``: endereço do servidor (127.0.0.1:5000).
- ``WEB_ALLOWED_HOSTS``: valores aceitos no cabeçalho Host, separados por
  vírgula (padrão: ``127.0.0.1:{porta}`` e ``localhost:{porta}``). Atrás
  de um proxy (nginx), inclua o domínio público.
- ``WEB_SECRET_KEY``: chave das sessões; se omitida, uma aleatória é gerada
  a cada reinício (as sessões abertas expiram).
- ``WEB_COOKIE_SECURE``: 1 quando o acesso é por HTTPS (atrás do nginx), para
  o cookie da sessão não trafegar em HTTP.
- ``DATABASE_URL``: banco PostgreSQL/PostGIS usado por SIMEX, Ameaça & Pressão
  e Floreser (envio → banco → S3; ver :mod:`imazongeo_upload.banco`).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
import zipfile
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from flask import Flask, jsonify, render_template, request, session
from shapely.geometry import shape
from werkzeug.datastructures import FileStorage
from werkzeug.middleware.proxy_fix import ProxyFix

from ..ameaca_pressao import nome_s3_ap
from ..banco.padrao import PADROES, Periodo
from ..config import LOG_FORMAT, load_env
from ..datasets import DATASETS, FORMATS
from ..s3 import usar_cliente_s3
from ..sad import SAD_CSV_DASHBOARD, identificar_camada_sad, processar_sad_zip
from ..simulador import SimuladorS3
from .conversion import extract_geojsons, process_zip

load_env()

HOST = os.getenv("WEB_HOST", "127.0.0.1")
PORT = int(os.getenv("WEB_PORT", "5000"))
ALLOWED_HOSTS = frozenset(
    h.strip() for h in os.getenv("WEB_ALLOWED_HOSTS", "").split(",") if h.strip()
) or frozenset({f"127.0.0.1:{PORT}", f"localhost:{PORT}"})

MAX_UPLOAD_BYTES = 1024 * 1024 * 1024  # 1 GB por requisição
MAX_JOBS = 20  # processamentos mantidos em memória
MAX_LOG_LINES = 3000

app = Flask(__name__)
app.secret_key = os.getenv("WEB_SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_HTTPONLY=True,
    # Atrás de HTTPS (WEB_COOKIE_SECURE=1), o cookie da sessão só vai por TLS
    SESSION_COOKIE_SECURE=os.getenv("WEB_COOKIE_SECURE", "").strip().lower()
    in ("1", "true", "on", "sim"),
)
# Atrás do nginx: usa Host, protocolo e prefixo (X-Forwarded-Prefix) do proxy,
# para a aplicação funcionar também sob um caminho, ex.: /upload/
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
lock = threading.Lock()
jobs: dict[str, dict[str, Any]] = {}


@app.before_request
def protect() -> Any:
    """Recusa hosts não permitidos e POSTs sem o token CSRF da sessão."""
    if request.host not in ALLOWED_HOSTS:
        return jsonify(error="Host não permitido."), 403
    if request.method == "POST" and not hmac.compare_digest(
        request.headers.get("X-CSRF-Token", ""),
        session.get("token", secrets.token_hex(32)),
    ):
        return jsonify(error="Recarregue a página para iniciar uma sessão."), 403
    return None


@app.get("/")
def index() -> str:
    """Página principal."""
    session.setdefault("token", secrets.token_hex(32))
    return render_template("index.html", token=session["token"])


def validate_zip(path: Path) -> None:
    """Recusa ZIPs com caminhos absolutos ou que saiam da pasta de destino."""
    with zipfile.ZipFile(path) as archive:
        for item in archive.infolist():
            name = PurePosixPath(item.filename.replace("\\", "/"))
            if name.is_absolute() or ".." in name.parts or ":" in item.filename:
                raise ValueError("ZIP contém um caminho inválido.")


def validate_sad_geojson(path: Path) -> set[tuple[int, int]]:
    """Valida o conteúdo antes de permitir qualquer operação no S3.

    Retorna os períodos (ano, mês) presentes no GeoJSON.
    """
    try:
        with path.open(encoding="utf-8-sig") as source:
            data = json.load(source)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("O arquivo SAD deve conter um GeoJSON válido.") from exc
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
        raise ValueError("O GeoJSON SAD deve ser uma FeatureCollection.")
    features = data.get("features")
    if not isinstance(features, list) or not features:
        raise ValueError("O GeoJSON SAD deve conter feições.")
    periods = set()
    for feature in features:
        try:
            if feature.get("type") != "Feature":
                raise ValueError()
            props = feature["properties"]
            year, month = float(props["ANO"]), float(props["MES"])
            if (
                not year.is_integer()
                or not month.is_integer()
                or not 1900 <= year <= 2100
                or not 1 <= month <= 12
            ):
                raise ValueError()
            geometry = shape(feature["geometry"])
            if (
                geometry.geom_type not in ("Polygon", "MultiPolygon")
                or geometry.is_empty
                or not geometry.is_valid
            ):
                raise ValueError()
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ValueError(
                "Cada feição SAD deve ter ANO/MES válidos e geometria Polygon "
                "ou MultiPolygon válida."
            ) from exc
        periods.add((int(year), int(month)))
    return periods


def _validate_form(form: Mapping[str, str]) -> dict[str, Any]:
    """Valida os campos do formulário (base, modo, período, bucket e senha)."""
    dataset = form.get("dataset")
    mode = form.get("mode", "real")
    op = form.get("operation", "download")
    fmt = form.get("format", "geojson")
    if dataset not in DATASETS or mode not in ("dry_run", "simulation", "real"):
        raise ValueError("Base ou modo inválido.")
    if op not in ("download", "dashboard") or fmt not in FORMATS:
        raise ValueError("Operação ou formato inválido.")
    year, month, quarter = (int(form.get(k, "0")) for k in ("year", "month", "quarter"))
    if not 1900 <= year <= 2100 or not 1 <= month <= 12 or not 1 <= quarter <= 4:
        raise ValueError("Período inválido.")
    bucket = form.get("bucket", "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise ValueError("Nome de bucket inválido.")
    if mode == "real":
        password = os.getenv("UPLOAD_PASSWORD", "")
        if not password or not hmac.compare_digest(password, form.get("password", "")):
            raise ValueError(
                "Senha de upload inválida ou UPLOAD_PASSWORD não configurada."
            )
        if form.get("confirm") != "on":
            raise ValueError("Confirme a atualização dos objetos no S3.")
        if not os.getenv("ACCESS_KEY") or not os.getenv("PRIVATE_KEY"):
            raise ValueError("Configure ACCESS_KEY e PRIVATE_KEY no .env da aplicação.")
    if dataset in PADROES and mode != "dry_run" and not os.getenv("DATABASE_URL"):
        raise ValueError("Configure DATABASE_URL no .env da aplicação.")
    return {
        "dataset": dataset,
        "mode": mode,
        "op": op,
        "fmt": fmt,
        "year": year,
        "month": month,
        "quarter": quarter,
        "bucket": bucket,
    }


def _normalize_sad_zip(
    archive: zipfile.ZipFile,
    form: Mapping[str, str],
    base: Path,
    sad_layers: set[tuple[str, str]],
    sad_periods: set[tuple[int, int]],
    sad_normalized: list[Path],
) -> None:
    """Valida os GeoJSONs de um ZIP do SAD e os regrava com o nome canônico."""
    members = [m for m in archive.infolist() if not m.is_dir()]
    if any(
        Path(m.filename).suffix.lower() in (".shp", ".shx", ".dbf", ".csv")
        for m in members
    ):
        raise ValueError("O ZIP do SAD deve conter GeoJSONs, não Shapefiles ou CSVs.")
    geojsons = [m for m in members if Path(m.filename).suffix.lower() == ".geojson"]
    if not geojsons:
        raise ValueError("O ZIP do SAD deve conter pelo menos um arquivo .geojson.")
    for member in geojsons:
        name = PurePosixPath(member.filename.replace("\\", "/")).name
        info = identificar_camada_sad(name)
        if info is None:
            tipo = form.get("sad_type", "")
            camada = form.get("sad_layer", "")
            if tipo not in SAD_CSV_DASHBOARD or camada not in SAD_CSV_DASHBOARD[tipo]:
                raise ValueError(
                    "Selecione o tipo de alerta e a camada SAD para os arquivos "
                    "com nome livre."
                )
            layer = (tipo, camada)
        else:
            layer = (info[0], info[1])
        if layer in sad_layers:
            raise ValueError("Envie apenas um GeoJSON por tipo e camada SAD.")
        sad_layers.add(layer)
        extracted = base / "validacao" / name
        extracted.parent.mkdir(exist_ok=True)
        with archive.open(member) as source, extracted.open("wb") as target:
            shutil.copyfileobj(source, target)
        periods = validate_sad_geojson(extracted)
        sad_periods.update(periods)
        start, end = min(periods), max(periods)
        canonical = (
            f"alertas_sad_{layer[0]}_{start[1]:02d}_{start[0]}_"
            f"{end[1]:02d}_{end[0]}_{layer[1]}.geojson"
        )
        normalized = base / f"sad_normalizado_{len(sad_normalized)}.zip"
        with zipfile.ZipFile(normalized, "w", zipfile.ZIP_DEFLATED) as output:
            output.write(extracted, canonical)
        sad_normalized.append(normalized)
        extracted.unlink()


def prepare(
    form: Mapping[str, str], uploads: Iterable[FileStorage], base: Path
) -> dict[str, Any]:
    """Valida o formulário e os arquivos enviados e prepara as opções do job."""
    options = _validate_form(form)
    dataset, op, year = options["dataset"], options["op"], options["year"]
    uploads = [f for f in uploads if f.filename]
    if dataset in PADROES:
        return _prepare_banco(options, uploads, base, form.get("public") == "on")
    if not uploads or (dataset != "sad" and len(uploads) != 1):
        raise ValueError(
            "Selecione um ZIP contendo GeoJSONs (o SAD aceita várias partes)."
        )
    paths: list[Path] = []
    sad_layers: set[tuple[str, str]] = set()
    sad_periods: set[tuple[int, int]] = set()
    sad_normalized: list[Path] = []
    annual_zip = dataset == "ameaca_pressao" and op == "dashboard"
    for i, upload in enumerate(uploads):
        if Path(upload.filename).suffix.lower() != ".zip":
            raise ValueError("Selecione arquivo .zip para esta operação.")
        if dataset == "sad":
            dest = base / f"parte_{i}.zip"
        elif annual_zip:
            dest = base / dataset / str(year) / f"ameaca_e_pressao_{year}.zip"
        else:
            dest = base / "entrada.zip"
        dest.parent.mkdir(parents=True, exist_ok=True)
        upload.save(dest)
        validate_zip(dest)
        if dataset == "sad":
            with zipfile.ZipFile(dest) as archive:
                _normalize_sad_zip(
                    archive, form, base, sad_layers, sad_periods, sad_normalized
                )
        paths.append(dest)
    if dataset == "sad":
        paths = sad_normalized
        if (
            form.get("all_months") != "on"
            and (year, options["month"]) not in sad_periods
        ):
            raise ValueError(
                "O GeoJSON não contém alertas para o mês e ano selecionados."
            )
    geojsons = extract_geojsons(paths[0], base / "entrada") if dataset != "sad" else []
    if annual_zip and any(not nome_s3_ap(p.name) for p in geojsons):
        raise ValueError(
            "Todos os GeoJSONs de Ameaça & Pressão devem ter nomes de categoria "
            "reconhecidos."
        )
    return {
        **options,
        "paths": paths,
        "geojsons": geojsons,
        "all_months": form.get("all_months") == "on",
        "dashboard_all": form.get("dashboard_all") == "on",
        "public": form.get("public") == "on",
    }


def _prepare_banco(
    options: dict[str, Any], uploads: list[FileStorage], base: Path, public: bool
) -> dict[str, Any]:
    """Arquivo único para o fluxo envio → banco → S3 (validado no job)."""
    aceitas = (".zip", ".geojson") + (
        (".csv",) if options["dataset"] == "floreser" else ()
    )
    if len(uploads) != 1:
        raise ValueError("Selecione um único arquivo com todas as camadas do período.")
    nome = PurePosixPath(uploads[0].filename.replace("\\", "/")).name
    if Path(nome).suffix.lower() not in aceitas:
        raise ValueError("Selecione um arquivo " + ", ".join(aceitas) + ".")
    dest = base / "envio" / f"entrada{Path(nome).suffix.lower()}"
    dest.parent.mkdir(parents=True)
    uploads[0].save(dest)
    if dest.suffix == ".zip":
        validate_zip(dest)
    trimestre = options["quarter"] if options["dataset"] == "ameaca_pressao" else None
    return {
        **options,
        "paths": [dest],
        "filename": nome,
        "periodo": Periodo(options["year"], trimestre),
        "public": public,
    }


class JobLog(logging.Handler):
    """Guarda no job os logs emitidos pela thread do processamento."""

    def __init__(self, job: dict[str, Any]) -> None:
        super().__init__()
        self.job = job
        self.worker = threading.get_ident()
        self.setFormatter(logging.Formatter(LOG_FORMAT, "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        """Acrescenta a mensagem ao job (mantém só as últimas linhas)."""
        if record.thread == self.worker:
            self.job["logs"].append(self.format(record))
            self.job["logs"] = self.job["logs"][-MAX_LOG_LINES:]


def _run(options: dict[str, Any], base: Path) -> None:
    dry = options["mode"] == "dry_run"
    if options["dataset"] in PADROES:
        from ..banco.fluxo import processar_envio

        processar_envio(
            options["paths"][0],
            options["dataset"],
            options["periodo"],
            bucket=options["bucket"],
            modo=options["mode"],
            public=options["public"],
            nome=options["filename"],
        )
    elif options["dataset"] == "sad":
        from ..banco.carga_sad import modo_banco

        processar_sad_zip(
            zips=options["paths"],
            bucket=options["bucket"],
            dry_run=dry,
            ano=options["year"],
            mes=options["month"],
            todos_meses=options["all_months"],
            dashboard_todos_meses=options["dashboard_all"],
            public=options["public"],
            banco=modo_banco(dry, options["mode"] == "simulation"),
        )
    else:
        process_zip(options, base)


def execute(
    job: dict[str, Any],
    options: dict[str, Any],
    temp: tempfile.TemporaryDirectory[str],
) -> None:
    """Executa o processamento em segundo plano e libera o lock ao terminar."""
    handler = JobLog(job)
    logger = logging.getLogger()
    level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        base = Path(temp.name)
        if options["mode"] == "simulation":
            simulator = SimuladorS3(base / "_s3")
            with usar_cliente_s3(simulator):
                _run(options, base)
            logging.info(simulator.relatorio())
        else:
            _run(options, base)
        logging.info("Processamento concluído. Modo: %s.", options["mode"])
        job["status"] = "done"
    except Exception as exc:
        logging.error("%s", exc)
        job["status"] = "error"
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
        temp.cleanup()
        lock.release()


@app.post("/api/jobs")
def start_job() -> Any:
    """Recebe os arquivos, valida e inicia o processamento."""
    if not lock.acquire(blocking=False):
        return jsonify(error="Já existe um processamento em andamento."), 409
    temp = tempfile.TemporaryDirectory(prefix="imazon_web_")
    try:
        options = prepare(request.form, request.files.getlist("files"), Path(temp.name))
        job_id = secrets.token_hex(16)
        job = {"status": "running", "logs": [], "owner": session["token"]}
        while len(jobs) >= MAX_JOBS:
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


@app.get("/api/jobs/<job_id>")
def job_status(job_id: str) -> Any:
    """Estado e logs de um processamento da sessão atual."""
    job = jobs.get(job_id)
    if not job or job["owner"] != session.get("token"):
        return jsonify(error="Processamento não encontrado."), 404
    return jsonify(status=job["status"], logs=job["logs"])


@app.errorhandler(413)
def too_large(_error: Exception) -> Any:
    """Resposta para envios acima de MAX_CONTENT_LENGTH."""
    return jsonify(error="O total de arquivos excede o limite de 1 GB."), 413


def main() -> None:
    """Servidor de desenvolvimento (use gunicorn em produção)."""
    app.run(host=HOST, port=PORT, debug=False)


if __name__ == "__main__":
    main()
