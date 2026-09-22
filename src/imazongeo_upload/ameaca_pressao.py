"""Ameaça & Pressão: dashboard com arquivos anuais por categoria e classe."""

from __future__ import annotations

import json
import logging
import re
import tempfile
import zipfile
from pathlib import Path

from .datasets import DATASETS, prefixo_dashboard
from .s3 import baixar_arquivo_s3, create_s3_client, enviar_arquivo_s3

_AP_EXTENSOES = (".geojson", ".json", ".zip")

# ameaca_e_pressao_{AAAA}_{categoria}_{ameaca|pressao}.geojson
_AP_NOME_RE = re.compile(
    r"^ameaca_e_pressao_\d{4}_\w+_(?:ameaca|pressao)\.geojson$", re.IGNORECASE
)


def nome_s3_ap(nome_arquivo: str) -> str | None:
    """Nome no S3 do GeoJSON de uma categoria de Ameaça & Pressão.

    Reconhece ``ameaca_e_pressao_{AAAA}_{categoria}_{ameaca|pressao}.geojson``
    (o mesmo padrão dos arquivos anuais do dashboard) e mantém o nome, para
    que cada categoria atualize sempre o mesmo objeto no S3. Retorna None se
    o nome não seguir o padrão.
    """
    nome = Path(nome_arquivo).name
    if not _AP_NOME_RE.match(nome):
        return None
    return nome[: -len(".geojson")] + ".geojson"


def _localizar_zip_ap(base_dir: Path, year: int) -> Path | None:
    """Localiza o ZIP anual em dados/ameaca_pressao/AAAA/ e subpastas."""
    raiz = base_dir / "ameaca_pressao" / str(year)
    padrao = f"ameaca_e_pressao_{year}.zip"

    candidato = raiz / padrao
    if candidato.exists():
        return candidato

    if raiz.exists():
        for sub in raiz.iterdir():
            if sub.is_dir() and (sub / padrao).exists():
                return sub / padrao
        zips = list(raiz.glob("*.zip"))
        if zips:
            return zips[0]

    return None


def _ano_trimestre_ap(feat: dict) -> tuple[int, int] | None:
    """(ano, trimestre) de uma feição de ameaça e pressão, ou None se faltar."""
    props = feat.get("properties") or {}
    try:
        return int(props["ano"]), int(props["trimestre"])
    except (KeyError, TypeError, ValueError):
        return None


def _ler_geojsons_ap(arquivo: Path) -> tuple[list[dict], dict | None]:
    """Feições de um GeoJSON (ou de todos os GeoJSONs de um ZIP) e o ``crs``.

    O ``crs`` é o do primeiro arquivo que tiver um.
    """
    if arquivo.suffix.lower() == ".zip":
        with zipfile.ZipFile(arquivo) as zf:
            colecoes = [
                json.loads(zf.read(n).decode("utf-8-sig"))
                for n in zf.namelist()
                if n.lower().endswith((".geojson", ".json"))
            ]
    else:
        with open(arquivo, encoding="utf-8-sig") as f:
            colecoes = [json.load(f)]
    features = [feat for colecao in colecoes for feat in colecao.get("features", [])]
    crs = next((colecao["crs"] for colecao in colecoes if "crs" in colecao), None)
    return features, crs


def processar_ameaca_pressao_dashboard(
    base_dir: Path,
    bucket: str,
    year: int,
    dry_run: bool,
    public: bool = True,
    arquivo: Path | None = None,
) -> None:
    """Atualiza o dashboard de Ameaça & Pressão.

    Um arquivo anual por categoria (dado) e classe (ameaça/pressão), com os
    trimestres do ano::

        s3://bucket/dashboard/ap/geojson/ameaca_e_pressao_{ano}_{dado}_{classe}.geojson

    Lê as feições do GeoJSON do trimestre (ou dos GeoJSONs de um ZIP), agrupa
    por ano + dado + classe e, em cada arquivo anual do S3, substitui os
    trimestres enviados e mantém os demais.

    arquivo: GeoJSON ou ZIP escolhido; se omitido, é usado o ZIP em
             {base_dir}/ameaca_pressao/AAAA/.
    """
    cfg = DATASETS["ameaca_pressao"]
    logging.info("=== Dashboard Ameaça & Pressão ===")

    if arquivo is None:
        arquivo = _localizar_zip_ap(base_dir, year)
        if arquivo is None:
            logging.warning(
                "ZIP de Ameaça & Pressão não encontrado em %s/ameaca_pressao/%d/.",
                base_dir,
                year,
            )
            return
    arquivo = Path(arquivo)
    if not arquivo.is_file():
        raise FileNotFoundError(
            f"Arquivo de Ameaça & Pressão não encontrado: {arquivo}"
        )
    if arquivo.suffix.lower() not in _AP_EXTENSOES:
        raise ValueError(
            "No dashboard de Ameaça & Pressão, envie o GeoJSON do trimestre "
            "ou um ZIP com GeoJSONs."
        )
    logging.info("Arquivo: %s", arquivo)

    features, crs = _ler_geojsons_ap(arquivo)
    grupos: dict[tuple[int, str, str], list[dict]] = {}
    ignoradas = 0
    for feat in features:
        props = feat.get("properties") or {}
        periodo = _ano_trimestre_ap(feat)
        if periodo is None or not props.get("dado") or not props.get("class"):
            ignoradas += 1
            continue
        ano, trimestre = periodo
        props.setdefault(
            "_source_file", f"ameaca_e_pressao_{trimestre}_trimestre_{ano}.geojson"
        )
        chave = (ano, str(props["dado"]), str(props["class"]))
        grupos.setdefault(chave, []).append(feat)
    if ignoradas:
        logging.warning(
            "%d feição(ões) sem ano, trimestre, dado ou class ignorada(s).", ignoradas
        )
    if not grupos:
        raise ValueError(
            "Nenhuma feição de ameaça e pressão (com ano, trimestre, dado e class) "
            f"em {arquivo.name}."
        )

    prefixo = prefixo_dashboard(cfg, "geojson")
    s3_client = None if dry_run else create_s3_client()

    with tempfile.TemporaryDirectory(prefix="imazon_ap_") as tmp_str:
        tmp = Path(tmp_str)
        for (ano, dado, classe), novas in sorted(grupos.items()):
            nome = f"ameaca_e_pressao_{ano}_{dado}_{classe}.geojson"
            key = prefixo + nome
            enviados = {_ano_trimestre_ap(f) for f in novas}
            trimestres = ", ".join(str(t) for _a, t in sorted(enviados))
            if dry_run:
                logging.info(
                    "[DRY RUN] %s: substituiria o(s) trimestre(s) %s (%d feições) "
                    "em s3://%s/%s",
                    nome,
                    trimestres,
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
                colecao = {
                    "type": "FeatureCollection",
                    "name": Path(nome).stem,
                    "features": [],
                }
                if crs:
                    colecao["crs"] = crs

            antigas = colecao.get("features", [])
            mantidas = [f for f in antigas if _ano_trimestre_ap(f) not in enviados]
            # Mantém os trimestres em ordem dentro do arquivo anual
            colecao["features"] = sorted(
                mantidas + novas, key=lambda f: _ano_trimestre_ap(f) or (0, 0)
            )
            logging.info(
                "%s: trimestre(s) %s — %d feição(ões) mantida(s), "
                "%d substituída(s), %d nova(s).",
                nome,
                trimestres,
                len(mantidas),
                len(antigas) - len(mantidas),
                len(novas),
            )

            arquivo_final = tmp / nome
            with open(arquivo_final, "w", encoding="utf-8") as f:
                json.dump(colecao, f, ensure_ascii=False)
            enviar_arquivo_s3(s3_client, arquivo_final, bucket, key, public)
