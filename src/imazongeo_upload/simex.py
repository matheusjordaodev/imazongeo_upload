"""SIMEX: dashboard com arquivos por camada, cada um com todos os anos."""

from __future__ import annotations

import json
import logging
import re
import tempfile
from pathlib import Path
from typing import Any

from .datasets import DATASETS, DatasetConfig, prefixo_dashboard
from .s3 import baixar_arquivo_s3, create_s3_client, enviar_arquivo_s3

# Camada no arquivo unificado do ano (início do nome em origem_arquivo ou
# source_layer, em minúsculas) → (nome no CSV, nome no GeoJSON) do dashboard.
# A ordem importa: "municipios" antes de "mun".
_SIMEX_CAMADAS = [
    ("imoveisrurais", "imoveisrurais", "imoveisrurais"),
    ("municipios", "municipios", "mun"),
    ("mun", "municipios", "mun"),
    ("assentamentos", "assentamentos", "assentamentos"),
    ("terraspndest", "terras_ndest", "TerrasNDest"),
    ("terrasndest", "terras_ndest", "TerrasNDest"),
    ("terras_ndest", "terras_ndest", "TerrasNDest"),
    ("ti", "ti", "TI"),
    ("uc", "uc", "UC"),
]

# Colunas que só identificam a origem de cada registro no arquivo unificado
_SIMEX_COLUNAS_ORIGEM = ("origem_arquivo", "origem_planilha", "source_layer")


def _camada_simex(registro: Any) -> tuple[str, str] | None:
    """Camada do dashboard de um registro (dict ou linha) do arquivo unificado.

    Usa a coluna origem_arquivo (ex.: simex_amz_2024_imoveisruraispri.xlsx)
    ou source_layer (ex.: TerrasNDest). Retorna (nome_csv, nome_geojson) ou
    None.
    """
    for campo in ("origem_arquivo", "source_layer"):
        valor = registro.get(campo)
        if not isinstance(valor, str) or not valor.strip():
            continue
        token = re.sub(
            r"^simex_amz(?:onia)?_(?:\d{4}_)?", "", Path(valor.strip()).stem.lower()
        )
        for inicio, nome_csv, nome_geojson in _SIMEX_CAMADAS:
            if token.startswith(inicio):
                return nome_csv, nome_geojson
    return None


def _ano_simex(registro: Any) -> int | None:
    """Ano de um registro (dict ou linha), pela coluna ano, Ano ou ANO."""
    for campo in ("ano", "Ano", "ANO"):
        try:
            return int(float(registro.get(campo)))
        except (TypeError, ValueError):
            continue
    return None


def processar_simex_dashboard(
    bucket: str,
    dry_run: bool,
    arquivo: Path,
    public: bool = True,
) -> None:
    """Atualiza o dashboard do SIMEX.

    Arquivos por camada (assentamentos, imóveis rurais, municípios, terras
    não destinadas, TI e UC), cada um com todos os anos::

        CSV     → s3://bucket/dashboard/simex/csv/simex_amazonia_PAMT_{camada}.csv
        GeoJSON → s3://bucket/dashboard/simex/geojson/simex_amz_PAMTM_{camada}.geojson

    Recebe o arquivo unificado do ano (o mesmo do download) em CSV ou
    GeoJSON, separa os registros por camada (origem_arquivo ou source_layer)
    e, em cada arquivo do S3, substitui os anos enviados e mantém os demais.
    O CSV atualiza os CSVs do dashboard; o GeoJSON, os mapas.
    """
    cfg = DATASETS["simex"]
    arquivo = Path(arquivo)
    if not arquivo.is_file():
        raise FileNotFoundError(f"Arquivo do SIMEX não encontrado: {arquivo}")
    ext = arquivo.suffix.lower()
    if ext not in (".csv", ".geojson", ".json"):
        raise ValueError(
            "No dashboard do SIMEX, envie o arquivo unificado do ano em CSV ou GeoJSON."
        )
    logging.info("=== Dashboard SIMEX: %s ===", arquivo.name)

    s3_client = None if dry_run else create_s3_client()
    with tempfile.TemporaryDirectory(prefix="imazon_simex_") as tmp_str:
        tmp = Path(tmp_str)
        if ext == ".csv":
            _simex_dashboard_csv(cfg, arquivo, bucket, s3_client, tmp, dry_run, public)
        else:
            _simex_dashboard_geojson(
                cfg, arquivo, bucket, s3_client, tmp, dry_run, public
            )


def _simex_dashboard_csv(
    cfg: DatasetConfig,
    arquivo: Path,
    bucket: str,
    s3_client: Any,
    tmp: Path,
    dry_run: bool,
    public: bool,
) -> None:
    """CSVs do dashboard do SIMEX a partir do CSV unificado do ano."""
    import pandas as pd

    df = pd.read_csv(arquivo, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    registros = df.to_dict("records")
    camadas = pd.Series(
        [_camada_simex(r) for r in registros], index=df.index, dtype=object
    )
    anos = pd.Series([_ano_simex(r) for r in registros], index=df.index, dtype=object)
    sem_camada = int(camadas.isna().sum())
    if sem_camada:
        logging.warning(
            "%d linha(s) sem camada reconhecida (origem_arquivo/source_layer) "
            "ignorada(s).",
            sem_camada,
        )
    if sem_camada == len(df):
        raise ValueError(
            f"{arquivo.name}: nenhuma linha com camada reconhecida em "
            "origem_arquivo ou source_layer."
        )

    prefixo = prefixo_dashboard(cfg, "csv")
    for nome_csv in sorted({c[0] for c in camadas.dropna()}):
        mascara = camadas.map(lambda c, n=nome_csv: c is not None and c[0] == n)
        mascara = mascara.astype(bool)
        novos = df[mascara]
        # Só as colunas preenchidas na camada, sem as de origem do arquivo
        # unificado
        colunas = [
            c
            for c in novos.columns
            if c not in _SIMEX_COLUNAS_ORIGEM and (novos[c] != "").any()
        ]
        novos = novos[colunas].assign(__source_file=arquivo.name)
        anos_enviados = sorted({a for a in anos[mascara] if a is not None})

        nome = f"simex_amazonia_PAMT_{nome_csv}.csv"
        key = prefixo + nome
        if dry_run:
            logging.info(
                "[DRY RUN] %s: substituiria o(s) ano(s) %s (%d linhas) em s3://%s/%s",
                nome,
                anos_enviados,
                len(novos),
                bucket,
                key,
            )
            continue

        csv_s3 = tmp / f"s3_{nome}"
        if baixar_arquivo_s3(s3_client, bucket, key, csv_s3):
            antigo = pd.read_csv(
                csv_s3, dtype=str, keep_default_na=False, encoding="utf-8-sig"
            )
            anos_antigo = pd.Series(
                [_ano_simex(r) for r in antigo.to_dict("records")],
                index=antigo.index,
                dtype=object,
            )
            manter = ~anos_antigo.isin(anos_enviados)
            final = pd.concat([antigo[manter], novos], ignore_index=True)
            logging.info(
                "%s: ano(s) %s — %d linha(s) mantida(s), %d substituída(s), "
                "%d nova(s).",
                nome,
                anos_enviados,
                int(manter.sum()),
                int((~manter).sum()),
                len(novos),
            )
        else:
            final = novos
            logging.info(
                "%s não existe no S3: será criado com %d linha(s).", nome, len(novos)
            )

        arquivo_final = tmp / nome
        final.to_csv(arquivo_final, index=False, encoding="utf-8-sig")
        enviar_arquivo_s3(s3_client, arquivo_final, bucket, key, public)
        csv_s3.unlink(missing_ok=True)


def _simex_dashboard_geojson(
    cfg: DatasetConfig,
    arquivo: Path,
    bucket: str,
    s3_client: Any,
    tmp: Path,
    dry_run: bool,
    public: bool,
) -> None:
    """GeoJSONs (mapas) do dashboard do SIMEX a partir do GeoJSON unificado."""
    with open(arquivo, encoding="utf-8-sig") as f:
        entrada = json.load(f)

    grupos: dict[str, list[dict]] = {}
    ignoradas = 0
    for feat in entrada.get("features", []):
        props = feat.get("properties") or {}
        camada = _camada_simex(props)
        if camada is None:
            ignoradas += 1
            continue
        feat["properties"] = {
            k: v for k, v in props.items() if k not in _SIMEX_COLUNAS_ORIGEM
        }
        grupos.setdefault(camada[1], []).append(feat)
    if ignoradas:
        logging.warning(
            "%d feição(ões) sem camada reconhecida (origem_arquivo/source_layer) "
            "ignorada(s).",
            ignoradas,
        )
    if not grupos:
        raise ValueError(
            f"{arquivo.name}: nenhuma feição com camada reconhecida em "
            "origem_arquivo ou source_layer."
        )

    prefixo = prefixo_dashboard(cfg, "geojson")
    for nome_geojson, novas in sorted(grupos.items()):
        nome = f"simex_amz_PAMTM_{nome_geojson}.geojson"
        key = prefixo + nome
        anos_enviados = sorted(
            {a for a in (_ano_simex(f["properties"]) for f in novas) if a is not None}
        )
        if dry_run:
            logging.info(
                "[DRY RUN] %s: substituiria o(s) ano(s) %s (%d feições) em s3://%s/%s",
                nome,
                anos_enviados,
                len(novas),
                bucket,
                key,
            )
            continue

        s3_atual = tmp / f"s3_{nome}"
        if baixar_arquivo_s3(s3_client, bucket, key, s3_atual):
            with open(s3_atual, encoding="utf-8-sig") as f:
                colecao = json.load(f)
        else:
            colecao = {"type": "FeatureCollection", "features": []}

        antigas = colecao.get("features", [])
        # Feições sem ano não podem ser associadas a um envio e são mantidas
        mantidas = [
            f
            for f in antigas
            if _ano_simex(f.get("properties") or {}) not in anos_enviados
        ]
        colecao["features"] = mantidas + novas
        logging.info(
            "%s: ano(s) %s — %d feição(ões) mantida(s), %d substituída(s), %d nova(s).",
            nome,
            anos_enviados,
            len(mantidas),
            len(antigas) - len(mantidas),
            len(novas),
        )

        arquivo_final = tmp / nome
        with open(arquivo_final, "w", encoding="utf-8") as f:
            json.dump(colecao, f, ensure_ascii=False)
        enviar_arquivo_s3(s3_client, arquivo_final, bucket, key, public)
        s3_atual.unlink(missing_ok=True)
        arquivo_final.unlink(missing_ok=True)
