"""Testes da interface web (Flask) com o S3 simulado."""

import io
import json
import os
import tempfile
import time
import unittest
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from imazongeo_upload.banco.padrao import Periodo
from imazongeo_upload.s3 import create_s3_client
from imazongeo_upload.web import server


@contextmanager
def capture_uploads() -> Iterator[dict[str, bytes]]:
    """Guarda o conteúdo de cada arquivo enviado ao S3 simulado, por chave."""
    captured: dict[str, bytes] = {}
    original = server.SimuladorS3.upload_file

    def capture(client, filename, bucket, key, **kwargs):
        captured[key] = Path(filename).read_bytes()
        return original(client, filename, bucket, key, **kwargs)

    with patch.object(server.SimuladorS3, "upload_file", capture):
        yield captured


class WebTests(unittest.TestCase):
    def sad_zip(
        self,
        properties=None,
        name="dados/alertas_sad_desmatamento_01_2025_municipios.geojson",
    ):
        content = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": properties
                    if properties is not None
                    else {"ANO": 2025, "MES": 1, "MUNICIPIO": "Belem"},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[-49, -2], [-48, -2], [-48, -1], [-49, -2]]],
                    },
                }
            ],
        }
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr(name, json.dumps(content))
        data.seek(0)
        return data, "sad.zip"

    def test_sad_converts_and_uploads_all_formats(self):
        with capture_uploads() as captured:
            result = self.finish(self.post(dataset="sad", files=self.sad_zip()))
        self.assertEqual(result["status"], "done", result["logs"])
        for fmt in ("geojson", "csv", "shapefile"):
            key = f"sad/{fmt}/sad_2025_01.zip"
            self.assertIn(key, captured)
            with zipfile.ZipFile(io.BytesIO(captured[key])) as archive:
                extensions = {Path(n).suffix for n in archive.namelist()}
                if fmt == "shapefile":
                    self.assertTrue(
                        {".shp", ".shx", ".dbf", ".prj"}.issubset(extensions)
                    )
                elif fmt == "csv":
                    self.assertIn("Belem", archive.read(archive.namelist()[0]).decode())
                else:
                    output = json.loads(archive.read(archive.namelist()[0]))
                    self.assertEqual(len(output["features"]), 1)
                    self.assertEqual(output["features"][0]["properties"]["ANO"], 2025)

    def test_sad_simulation_writes_database_in_simulation_mode(self):
        calls = []

        def fake(arquivos, modo, database_url=None):
            calls.append(([(a.tipo, a.camada) for a in arquivos], modo))

        with (
            patch.dict(os.environ, {"DATABASE_URL": "postgresql:///teste"}),
            patch("imazongeo_upload.banco.carga_sad.gravar", fake),
        ):
            result = self.finish(self.post(dataset="sad"))
        self.assertEqual(result["status"], "done", result["logs"])
        self.assertEqual(calls, [([("desmatamento", "municipios")], "simulation")])

    def sad_gpkg(self, nome="alertas_sad_desmatamento_08_2026_municipios.gpkg"):
        """ZIP com a camada em GeoPackage, como vem do export atual do SAD."""
        import geopandas as gpd
        from shapely.geometry import box

        with tempfile.TemporaryDirectory() as tmp:
            caminho = Path(tmp) / nome
            gpd.GeoDataFrame(
                {
                    "ALERTA": ["desmatamento"],
                    "MES": [8],
                    "ANO": [2026],
                    "SENSOR": ["Sentinel-2"],
                    "ESTADO": ["PA"],
                    "AREAKM2": [1.2],
                    "MUNICIPIOS": ["Altamira"],
                },
                geometry=[box(-52, -5, -51.99, -4.99)],
                crs=4326,
            ).to_file(caminho, driver="GPKG")
            dados = io.BytesIO()
            with zipfile.ZipFile(dados, "w") as archive:
                archive.write(caminho, nome)
        dados.seek(0)
        return dados, "sad_gpkg.zip"

    def test_sad_accepts_geopackage(self):
        with capture_uploads() as captured:
            result = self.finish(
                self.post(dataset="sad", year="2026", month="8", files=self.sad_gpkg())
            )
        self.assertEqual(result["status"], "done", result["logs"])
        self.assertIn("sad/geojson/sad_2026_08.zip", set(captured))
        with zipfile.ZipFile(
            io.BytesIO(captured["sad/geojson/sad_2026_08.zip"])
        ) as archive:
            self.assertEqual(
                archive.namelist(),
                ["alertas_sad_desmatamento_08_2026_municipios.geojson"],
            )

    def test_sad_rejeita_gpkg_sem_ano_mes(self):
        import geopandas as gpd
        from shapely.geometry import box

        with tempfile.TemporaryDirectory() as tmp:
            caminho = Path(tmp) / "alertas_sad_desmatamento_08_2026_municipios.gpkg"
            gpd.GeoDataFrame(
                {"ALERTA": ["desmatamento"]},
                geometry=[box(-52, -5, -51.99, -4.99)],
                crs=4326,
            ).to_file(caminho, driver="GPKG")
            dados = io.BytesIO()
            with zipfile.ZipFile(dados, "w") as archive:
                archive.write(caminho, caminho.name)
        dados.seek(0)
        resposta = self.post(
            dataset="sad", year="2026", month="8", files=(dados, "sad.zip")
        )
        self.assertEqual(resposta.status_code, 400)
        self.assertIn("ANO", resposta.json["error"])

    def test_estaticos_com_versao(self):
        html = self.client.get("/", base_url="http://localhost:5000").get_data(
            as_text=True
        )
        self.assertRegex(html, r"static/app\.js\?v=[0-9a-f]+")
        self.assertRegex(html, r"static/style\.css\?v=[0-9a-f]+")

    def test_pagina_sem_cache(self):
        """A página não pode vir do cache: leva o token e o endereço da API."""
        resposta = self.client.get("/", base_url="http://localhost:5000")
        self.assertEqual(resposta.headers.get("Cache-Control"), "no-store")

    def test_sad_rejects_zip_without_geojson(self):
        self.assertEqual(
            self.post(
                dataset="sad", files=self.sad_zip(name="alertas.shp")
            ).status_code,
            400,
        )

    def test_sad_rejects_invalid_attributes(self):
        self.assertEqual(
            self.post(
                dataset="sad", files=self.sad_zip(properties={"ANO": 2025})
            ).status_code,
            400,
        )

    def test_sad_rejects_missing_period(self):
        self.assertEqual(
            self.post(dataset="sad", month="2", files=self.sad_zip()).status_code, 400
        )

    def test_sad_requires_zip(self):
        self.assertEqual(
            self.post(
                dataset="sad", files=(io.BytesIO(b"{}"), "sad.geojson")
            ).status_code,
            400,
        )

    def test_sad_accepts_arbitrary_filename(self):
        result = self.finish(
            self.post(
                dataset="sad",
                files=self.sad_zip(name="minha pasta/qualquer nome.geojson"),
                sad_type="degradacao",
                sad_layer="municipios",
            )
        )
        self.assertEqual(result["status"], "done", result["logs"])
        self.assertIn(
            "alertas_sad_degradacao_municipios.csv", "\n".join(result["logs"])
        )

    def test_sad_free_name_needs_classification(self):
        response = self.post(dataset="sad", files=self.sad_zip(name="livre.geojson"))
        self.assertEqual(response.status_code, 400)
        self.assertIn("tipo de alerta", response.json["error"])

    def setUp(self):
        # Independe de WEB_ALLOWED_HOSTS/WEB_PORT definidos no .env local
        hosts = patch.object(server, "ALLOWED_HOSTS", {"localhost:5000"})
        hosts.start()
        self.addCleanup(hosts.stop)
        # Nunca usa o banco configurado no .env local
        banco = patch.dict(os.environ, {"DATABASE_URL": ""})
        banco.start()
        self.addCleanup(banco.stop)
        self.client = server.app.test_client()
        self.client.get("/", base_url="http://localhost:5000")
        with self.client.session_transaction() as session:
            self.token = session["token"]

    def post(self, **changes):
        data = {
            "dataset": "floreser",
            "mode": "simulation",
            "operation": "download",
            "format": "csv",
            "year": "2025",
            "month": "1",
            "quarter": "1",
            "bucket": "imazongeo3-web",
            "files": self.sad_zip(),
            **changes,
        }
        return self.client.post(
            "/api/jobs",
            data=data,
            base_url="http://localhost:5000",
            headers={"X-CSRF-Token": self.token},
        )

    def finish(self, response):
        self.assertEqual(response.status_code, 202, response.json)
        for _ in range(200):
            result = self.client.get(
                "/api/jobs/" + response.json["id"], base_url="http://localhost:5000"
            )
            if result.json["status"] != "running" and not server.lock.locked():
                return result.json
            time.sleep(0.025)
        self.fail("Tempo limite excedido")

    def floreser_csv(self):
        data = io.BytesIO(
            "ano,cod_uf,estado,cod_mun,municipio,idade,area_ha\n"
            "2025,11,Rondônia,1100205,Porto Velho,1,10.5\n".encode()
        )
        return data, "floreser_2025.csv"

    def test_banco_preview_does_not_touch_database_or_aws(self):
        with (
            patch(
                "imazongeo_upload.banco.db.conectar",
                side_effect=AssertionError("banco"),
            ),
            patch(
                "imazongeo_upload.s3._novo_cliente_boto3",
                side_effect=AssertionError("AWS real"),
            ),
        ):
            result = self.finish(self.post(mode="dry_run", files=self.floreser_csv()))
        self.assertEqual(result["status"], "done", result["logs"])
        self.assertIn(
            "[PRÉVIA] Publicaria s3://imazongeo3-web/floreser/csv/floreser_2025.csv",
            "\n".join(result["logs"]),
        )

    def test_banco_simulation_uses_simulated_s3(self):
        calls = []

        def fake(path, dataset, periodo, **kwargs):
            calls.append((Path(path).suffix, dataset, periodo, kwargs))
            self.assertIsInstance(create_s3_client(), server.SimuladorS3)

        with (
            patch.dict(os.environ, {"DATABASE_URL": "postgresql:///teste"}),
            patch("imazongeo_upload.banco.fluxo.processar_envio", fake),
        ):
            result = self.finish(
                self.post(
                    dataset="ameaca_pressao",
                    quarter="3",
                    files=(io.BytesIO(b"{}"), "ap.geojson"),
                )
            )
        self.assertEqual(result["status"], "done", result["logs"])
        self.assertEqual(
            calls,
            [
                (
                    ".geojson",
                    "ameaca_pressao",
                    Periodo(2025, 3),
                    {
                        "bucket": "imazongeo3-web",
                        "modo": "simulation",
                        "public": False,
                        "nome": "ap.geojson",
                    },
                )
            ],
        )

    def test_banco_requires_database_url(self):
        with patch.dict(os.environ, {"DATABASE_URL": ""}):
            response = self.post(files=self.floreser_csv())
        self.assertEqual(response.status_code, 400)
        self.assertIn("DATABASE_URL", response.json["error"])

    def test_invalid_period(self):
        self.assertEqual(self.post(month="13").status_code, 400)

    def test_banco_accepted_files(self):
        csv = self.floreser_csv
        for dataset, files, status in [
            ("simex", csv(), 400),  # CSV só no Floreser
            ("simex", (io.BytesIO(b"x"), "dados.txt"), 400),
            ("floreser", csv(), 202),
        ]:
            with self.subTest(dataset=dataset, name=files[1]):
                response = self.post(dataset=dataset, mode="dry_run", files=files)
                self.assertEqual(response.status_code, status, response.json)
                if status == 202:
                    self.finish(response)
        response = self.post(
            dataset="simex",
            mode="dry_run",
            files=[self.sad_zip(), self.sad_zip()],
        )
        self.assertEqual(response.status_code, 400)

    def test_banco_invalid_upload_reports_error(self):
        result = self.finish(
            self.post(
                dataset="simex",
                mode="dry_run",
                files=self.sad_zip(name="qualquer.geojson"),
            )
        )
        self.assertEqual(result["status"], "error")
        self.assertIn("camada SIMEX não identificada", "\n".join(result["logs"]))

    def test_real_requires_password(self):
        with patch.dict(os.environ, {"UPLOAD_PASSWORD": "test-password"}):
            self.assertEqual(self.post(mode="real").status_code, 400)

    def test_servido_sob_um_caminho(self):
        # Atrás do nginx em /upload/: a página precisa apontar para o prefixo
        pagina = self.client.get(
            "/",
            base_url="http://localhost:5000",
            headers={"X-Forwarded-Prefix": "/upload"},
        )
        html = pagina.get_data(as_text=True)
        self.assertIn('data-api="/upload/api/jobs"', html)
        self.assertIn("/upload/static/app.js", html)
        self.assertIn("/upload/static/style.css", html)
        # O nginx tira o prefixo antes de repassar, então a API segue em /api/jobs
        resposta = self.post(
            mode="dry_run", dataset="floreser", files=self.floreser_csv()
        )
        self.assertEqual(resposta.status_code, 202, resposta.json)
        self.finish(resposta)

    def test_csrf(self):
        self.assertEqual(
            self.client.post("/api/jobs", base_url="http://localhost:5000").status_code,
            403,
        )

    def test_zip_traversal_rejected(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("../escape.shp", "invalid")
        data.seek(0)
        self.assertEqual(
            self.post(dataset="sad", files=(data, "sad.zip")).status_code, 400
        )

    def test_busy(self):
        server.lock.acquire()
        try:
            self.assertEqual(self.post().status_code, 409)
        finally:
            server.lock.release()


if __name__ == "__main__":
    unittest.main()
