"""Fluxo envio → banco → S3 com PostgreSQL/PostGIS.

Roda só com BANCO_TEST_DATABASE_URL definido. APAGA o esquema imazongeo desse
banco: use um banco descartável, ex.:

    createdb imazongeo_teste && psql -d imazongeo_teste -c "CREATE EXTENSION postgis"
    export BANCO_TEST_DATABASE_URL=postgresql:///imazongeo_teste
    pytest tests/test_banco_integracao.py
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from shapely.geometry import box

from banco_fixtures import csv_floreser, geojson_ap, zip_simex
from imazongeo_upload.banco import db, espelho, fluxo
from imazongeo_upload.banco.padrao import Periodo, padrao
from imazongeo_upload.s3 import usar_cliente_s3
from imazongeo_upload.simulador import SimuladorS3

URL = os.getenv("BANCO_TEST_DATABASE_URL")
BUCKET = "bucket-teste"


@unittest.skipUnless(
    URL, "defina BANCO_TEST_DATABASE_URL (banco descartável com PostGIS)"
)
class TestFluxoBanco(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        with db.conectar(URL) as conn:
            with conn, conn.cursor() as cur:
                cur.execute("DROP SCHEMA IF EXISTS imazongeo CASCADE")
            db.criar_schema(conn)
        self.s3 = SimuladorS3(self.dir / "_s3")
        contexto = usar_cliente_s3(self.s3)
        contexto.__enter__()
        self.addCleanup(contexto.__exit__, None, None, None)

    def tearDown(self):
        self.tmp.cleanup()

    def consulta(self, sql: str, *valores) -> list[tuple]:
        with db.conectar(URL) as conn, conn, conn.cursor() as cur:
            cur.execute(sql, valores)
            return cur.fetchall()

    def enviar(self, caminho: Path, dataset: str, periodo: Periodo, modo="real"):
        return fluxo.processar_envio(
            caminho, dataset, periodo, bucket=BUCKET, modo=modo, database_url=URL
        )

    def objeto_s3(self, chave: str) -> Path:
        return self.dir / "_s3" / BUCKET / chave

    def test_envio_real_grava_e_publica(self):
        r = self.enviar(zip_simex(self.dir), "simex", Periodo(2025))
        p = padrao("simex")
        self.assertEqual(
            sorted(r.publicados),
            sorted(p.chave_s3(Periodo(2025), f) for f in p.formatos),
        )
        csv = pd.read_csv(
            self.objeto_s3("simex/csv/simex_2025.csv"), encoding="utf-8-sig"
        )
        self.assertEqual(list(csv.columns), p.nomes)
        self.assertEqual(len(csv), 6)
        # area_ha vem da geometria (igual à área geodésica do PostGIS)
        self.assertEqual(
            self.consulta(
                "SELECT count(*), bool_and(abs(area_ha - ST_Area(geom::geography)"
                " / 1e4) < 1e-3) FROM imazongeo.simex_exploracao"
            ),
            [(6, True)],
        )
        self.assertEqual(
            self.consulta(
                "SELECT DISTINCT ST_SRID(geom), GeometryType(geom) "
                "FROM imazongeo.simex_exploracao"
            ),
            [(4326, "MULTIPOLYGON")],
        )
        self.assertEqual(
            self.consulta(
                "SELECT count(*), bool_and(bucket = %s) FROM imazongeo.publicacao "
                "WHERE carga_id = %s",
                BUCKET,
                r.carga_id,
            ),
            [(3, True)],
        )
        self.assertEqual(self.s3.acl_ops, [])  # sem --public

    def test_reenvio_substitui_o_periodo(self):
        self.enviar(zip_simex(self.dir, gleba="Gleba A"), "simex", Periodo(2025))
        self.enviar(zip_simex(self.dir, gleba="Gleba B"), "simex", Periodo(2025))
        self.assertEqual(
            self.consulta(
                "SELECT count(*), count(DISTINCT territorio) FILTER "
                "(WHERE territorio LIKE 'Gleba%%') FROM imazongeo.simex_exploracao"
            ),
            [(6, 1)],
        )
        self.assertEqual(
            self.consulta(
                "SELECT vigente, substituida_em IS NOT NULL FROM imazongeo.carga "
                "ORDER BY id"
            ),
            [(False, True), (True, False)],
        )
        csv = pd.read_csv(
            self.objeto_s3("simex/csv/simex_2025.csv"), encoding="utf-8-sig"
        )
        self.assertIn("Gleba B", set(csv["territorio"]))
        self.assertNotIn("Gleba A", set(csv["territorio"]))

    def test_simulacao_nao_altera_o_banco(self):
        r = self.enviar(csv_floreser(self.dir), "floreser", Periodo(2024), "simulation")
        self.assertEqual(r.publicados, ["floreser/csv/floreser_2024.csv"])
        self.assertTrue(self.objeto_s3("floreser/csv/floreser_2024.csv").exists())
        self.assertEqual(self.consulta("SELECT count(*) FROM imazongeo.carga"), [(0,)])
        self.assertEqual(
            self.consulta("SELECT count(*) FROM imazongeo.floreser_municipio"), [(0,)]
        )

    def test_ap_areas_compartilhadas_entre_trimestres(self):
        self.enviar(
            geojson_ap(self.dir, trimestre=2), "ameaca_pressao", Periodo(2025, 2)
        )
        self.enviar(
            geojson_ap(self.dir, trimestre=3), "ameaca_pressao", Periodo(2025, 3)
        )
        # Mesma geometria nos 2 trimestres e 8 rankings: uma área só
        self.assertEqual(
            self.consulta("SELECT count(*) FROM imazongeo.ap_area_protegida"), [(1,)]
        )
        # Nova geometria no T3: a antiga continua (usada no T2)
        outra = box(-52, -5, -51.5, -4)
        self.enviar(
            geojson_ap(self.dir, trimestre=3, geom=outra),
            "ameaca_pressao",
            Periodo(2025, 3),
        )
        self.assertEqual(
            self.consulta("SELECT count(*) FROM imazongeo.ap_area_protegida"), [(2,)]
        )
        # Reenvio do T2 com a nova geometria: a antiga fica órfã e é removida
        self.enviar(
            geojson_ap(self.dir, trimestre=2, geom=outra),
            "ameaca_pressao",
            Periodo(2025, 2),
        )
        self.assertEqual(
            self.consulta("SELECT count(*) FROM imazongeo.ap_area_protegida"), [(1,)]
        )
        self.assertEqual(
            self.consulta(
                "SELECT trimestre, count(*) FROM imazongeo.vw_ameaca_pressao "
                "GROUP BY 1 ORDER BY 1"
            ),
            [(2, 8), (3, 8)],
        )

    CERTO = "APA Arquipélago do Marajó"
    PERDIDO = "APA Arquip\ufffdlago do Maraj\ufffd"

    def enviar_ap(self, ano: int, trimestre: int, nome: str) -> None:
        self.enviar(
            geojson_ap(self.dir, ano=ano, trimestre=trimestre, area_nome=nome),
            "ameaca_pressao",
            Periodo(ano, trimestre),
        )

    def nomes_ap(self) -> list[tuple]:
        return self.consulta(
            "SELECT DISTINCT nome FROM imazongeo.vw_ameaca_pressao ORDER BY 1"
        )

    def test_ap_letra_perdida_corrigida_pelo_nome_do_banco(self):
        self.enviar_ap(2025, 2, self.CERTO)
        self.enviar_ap(2025, 3, self.PERDIDO)
        self.assertEqual(self.nomes_ap(), [(self.CERTO,)])
        csv = pd.read_csv(
            self.objeto_s3("ameaca_e_pressao/csv/ameaca_e_pressao_2025_t3.csv"),
            encoding="utf-8-sig",
        )
        self.assertEqual(set(csv["nome"]), {self.CERTO})

    def test_ap_letra_perdida_gravada_antes_do_nome_correto(self):
        # Ordem do histórico: o trimestre com "�" chega antes
        self.enviar_ap(2021, 1, self.PERDIDO)
        self.assertEqual(self.nomes_ap(), [(self.PERDIDO,)])
        self.enviar_ap(2021, 2, self.CERTO)
        self.assertEqual(self.nomes_ap(), [(self.CERTO,)])
        self.assertEqual(
            self.consulta("SELECT count(*) FROM imazongeo.ap_area_protegida"), [(1,)]
        )

    def test_ap_reenvio_atualiza_atributos_da_area(self):
        self.enviar_ap(2025, 2, self.CERTO)
        self.consulta(
            "UPDATE imazongeo.ap_area_protegida SET uso = 'Terra Indigena' RETURNING 1"
        )
        self.enviar_ap(2025, 2, self.CERTO)
        self.assertEqual(
            self.consulta("SELECT uso FROM imazongeo.ap_area_protegida"),
            [("Terra Indígena",)],
        )

    def test_republicar_todos_os_periodos(self):
        self.enviar(csv_floreser(self.dir, 2023), "floreser", Periodo(2023))
        self.enviar(csv_floreser(self.dir, 2024), "floreser", Periodo(2024))
        chaves = fluxo.republicar(
            "floreser", None, bucket=BUCKET, modo="real", database_url=URL
        )
        self.assertEqual(
            chaves, ["floreser/csv/floreser_2023.csv", "floreser/csv/floreser_2024.csv"]
        )
        self.assertEqual(
            self.consulta("SELECT count(*) FROM imazongeo.publicacao"), [(4,)]
        )
        with self.assertRaisesRegex(ValueError, "não está no banco"):
            fluxo.republicar(
                "floreser",
                [Periodo(2000)],
                bucket=BUCKET,
                modo="real",
                database_url=URL,
            )

    def test_importar_legado(self):
        destino = self.dir / "espelho"
        legado = espelho.ArquivoLegado(
            "floreser",
            Periodo(2024),
            "csv",
            espelho.chave_legada("floreser", Periodo(2024), "csv"),
        )
        (destino / "floreser").mkdir(parents=True)
        csv_floreser(destino / "floreser").rename(destino / legado.chave)
        espelho.gravar_manifesto([legado], destino)

        def importar():
            return fluxo.importar_legado(destino, ["floreser"], database_url=URL)

        self.assertEqual(importar(), {"floreser": 2})
        self.assertEqual(importar(), {})  # mesmo arquivo: pulado
        self.assertEqual(
            self.consulta("SELECT origem, arquivo FROM imazongeo.carga"),
            [("s3_legado", "floreser/floreser_2024.csv")],
        )
        # Um envio pela aplicação nunca é sobrescrito pelo legado
        self.enviar(csv_floreser(self.dir, area=99.0), "floreser", Periodo(2024))
        self.assertEqual(
            fluxo.importar_legado(destino, ["floreser"], forcar=True, database_url=URL),
            {},
        )
        self.assertEqual(
            self.consulta("SELECT max(area_ha) FROM imazongeo.floreser_municipio"),
            [(99.0,)],
        )


if __name__ == "__main__":
    unittest.main()
