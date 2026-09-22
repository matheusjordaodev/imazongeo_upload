"""Espelho local dos arquivos legados publicados no S3 (antes do banco).

Serve para importar o histórico para o banco. O bucket não permite listagem
anônima; os arquivos são descobertos testando (HEAD) os nomes que os
dashboards montam:

- SIMEX:            simex/{geojson,csv,shapefile}/simex_unificado_AAAA.{geojson,csv,zip}
- Ameaça & Pressão: ameaca_e_pressao/{...}/ameaca_e_pressao_T_trimestre_AAAA.{...}
- Floreser:         floreser/floreser_AAAA.csv

O caminho local repete a chave do S3. ``manifesto.json`` guarda tamanho, ETag e
data de modificação de cada arquivo.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from .padrao import FORMATOS, Periodo, padrao

S3_PUBLICO = "https://imazongeo3-web.s3.sa-east-1.amazonaws.com"
MANIFESTO = "manifesto.json"
DESTINO_PADRAO = Path("dados") / "espelho_s3"

# Primeiro ano a testar e formatos publicados, por dataset
_ANO_INICIAL = {"simex": 2005, "ameaca_pressao": 2019, "floreser": 1985}
_FORMATOS_LEGADOS = {
    "simex": ("geojson", "csv", "shapefile"),
    "ameaca_pressao": ("geojson", "csv", "shapefile"),
    "floreser": ("csv",),
}
# Ordem de preferência do arquivo usado para importar cada período
PREFERENCIA_FORMATO = ("geojson", "shapefile", "csv")


def chave_legada(dataset: str, periodo: Periodo, formato: str) -> str:
    pasta, ext = FORMATOS[formato]
    if dataset == "simex":
        return f"simex/{pasta}/simex_unificado_{periodo.ano}.{ext}"
    if dataset == "ameaca_pressao":
        nome = f"ameaca_e_pressao_{periodo.trimestre}_trimestre_{periodo.ano}"
        return f"ameaca_e_pressao/{pasta}/{nome}.{ext}"
    if dataset == "floreser":
        return f"floreser/floreser_{periodo.ano}.csv"
    raise ValueError(f"Dataset sem arquivos legados: {dataset}")


@dataclass
class ArquivoLegado:
    dataset: str
    periodo: Periodo
    formato: str
    chave: str
    tamanho: int | None = None
    etag: str | None = None
    modificado_em: datetime | None = None
    baixado_em: datetime | None = None

    @property
    def url(self) -> str:
        return f"{S3_PUBLICO}/{self.chave}"

    def para_dict(self) -> dict:
        return {
            "dataset": self.dataset,
            "ano": self.periodo.ano,
            "trimestre": self.periodo.trimestre,
            "mes": self.periodo.mes,
            "formato": self.formato,
            "chave": self.chave,
            "tamanho": self.tamanho,
            "etag": self.etag,
            "modificado_em": self.modificado_em.isoformat()
            if self.modificado_em
            else None,
            "baixado_em": self.baixado_em.isoformat() if self.baixado_em else None,
        }

    @classmethod
    def de_dict(cls, d: dict) -> ArquivoLegado:
        def data(v: str | None) -> datetime | None:
            return datetime.fromisoformat(v) if v else None

        return cls(
            dataset=d["dataset"],
            periodo=Periodo(d["ano"], d.get("trimestre"), d.get("mes")),
            formato=d["formato"],
            chave=d["chave"],
            tamanho=d.get("tamanho"),
            etag=d.get("etag"),
            modificado_em=data(d.get("modificado_em")),
            baixado_em=data(d.get("baixado_em")),
        )


def candidatos(
    datasets: list[str], ano_inicial: int, ano_final: int
) -> list[ArquivoLegado]:
    saida = []
    for slug in datasets:
        trimestral = padrao(slug).periodicidade == "trimestral"
        for ano in range(max(ano_inicial, _ANO_INICIAL[slug]), ano_final + 1):
            for t in (1, 2, 3, 4) if trimestral else (None,):
                periodo = Periodo(ano, t)
                for formato in _FORMATOS_LEGADOS[slug]:
                    chave = chave_legada(slug, periodo, formato)
                    saida.append(ArquivoLegado(slug, periodo, formato, chave))
    return saida


def _head(arquivo: ArquivoLegado) -> ArquivoLegado | None:
    req = urllib.request.Request(arquivo.url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            arquivo.tamanho = int(resp.headers["Content-Length"])
            arquivo.etag = (resp.headers.get("ETag") or "").strip('"') or None
            lm = resp.headers.get("Last-Modified")
            arquivo.modificado_em = parsedate_to_datetime(lm) if lm else None
            return arquivo
    except urllib.error.HTTPError as e:
        # Sem permissão de listagem, o S3 responde 403 (e não 404) a chaves inexistentes
        if e.code in (403, 404):
            return None
        raise


def descobrir(
    datasets: list[str], ano_inicial: int = 1985, ano_final: int | None = None
) -> list[ArquivoLegado]:
    """Arquivos legados que existem no S3, com tamanho, ETag e data."""
    lista = candidatos(datasets, ano_inicial, ano_final or date.today().year)
    logging.info("Verificando %d nomes candidatos no S3...", len(lista))
    with ThreadPoolExecutor(max_workers=16) as pool:
        encontrados = [a for a in pool.map(_head, lista) if a is not None]
    logging.info("%d arquivos encontrados.", len(encontrados))
    return encontrados


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def _md5(caminho: Path) -> str:
    h = hashlib.md5()
    with open(caminho, "rb") as f:
        for bloco in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloco)
    return h.hexdigest()


def atualizado(
    arquivo: ArquivoLegado, alvo: Path, etag_anterior: str | None = None
) -> bool:
    """O arquivo local corresponde ao objeto atual do S3?

    O ETag de um upload simples é o MD5 do conteúdo. No multipart ('md5-N')
    não é verificável localmente; compara-se com o ETag do último download.
    """
    if not alvo.exists() or alvo.stat().st_size != arquivo.tamanho:
        return False
    if not arquivo.etag:
        return True
    if "-" not in arquivo.etag:
        return _md5(alvo) == arquivo.etag
    return etag_anterior in (None, arquivo.etag)


def baixar_arquivo(
    arquivo: ArquivoLegado,
    destino: Path,
    tentativas: int = 3,
    etag_anterior: str | None = None,
) -> tuple[Path, bool]:
    """Baixa o arquivo se necessário. Retorna (caminho, se foi baixado agora)."""
    alvo = destino / arquivo.chave
    if atualizado(arquivo, alvo, etag_anterior):
        return alvo, False
    alvo.parent.mkdir(parents=True, exist_ok=True)
    parcial = alvo.with_name(alvo.name + ".part")
    for tentativa in range(1, tentativas + 1):
        try:
            with (
                urllib.request.urlopen(arquivo.url, timeout=120) as resp,
                open(parcial, "wb") as out,
            ):
                shutil.copyfileobj(resp, out, length=1024 * 1024)
            tamanho = parcial.stat().st_size
            if arquivo.tamanho is not None and tamanho != arquivo.tamanho:
                raise OSError(f"tamanho {tamanho} difere do S3 ({arquivo.tamanho})")
            parcial.replace(alvo)
            logging.info("Baixado: %s (%.1f MB)", arquivo.chave, tamanho / 1e6)
            return alvo, True
        except Exception as e:  # noqa: BLE001 - nova tentativa em qualquer falha de rede
            logging.warning(
                "Falha ao baixar %s (%d/%d): %s",
                arquivo.chave,
                tentativa,
                tentativas,
                e,
            )
            if tentativa == tentativas:
                parcial.unlink(missing_ok=True)
                raise
            time.sleep(2 * tentativa)
    raise AssertionError("inalcançável")


def baixar_todos(arquivos: list[ArquivoLegado], destino: Path) -> Path:
    """Baixa o que mudou e atualiza o manifesto. Retorna o caminho do manifesto."""
    destino.mkdir(parents=True, exist_ok=True)
    anteriores = {a.chave: a for a in ler_manifesto(destino)}
    logging.info(
        "Verificando/baixando %d arquivos (%.2f GB) em %s",
        len(arquivos),
        sum(a.tamanho or 0 for a in arquivos) / 1e9,
        destino,
    )

    def baixar(a: ArquivoLegado) -> bool:
        anterior = anteriores.get(a.chave)
        _, baixou = baixar_arquivo(a, destino, etag_anterior=anterior and anterior.etag)
        agora = datetime.now(timezone.utc)
        a.baixado_em = agora if baixou or not anterior else anterior.baixado_em or agora
        return baixou

    with ThreadPoolExecutor(max_workers=4) as pool:
        novos = sum(pool.map(baixar, arquivos))
    logging.info(
        "%d baixados, %d já estavam atualizados.", novos, len(arquivos) - novos
    )
    return gravar_manifesto(arquivos, destino)


def gravar_manifesto(arquivos: list[ArquivoLegado], destino: Path) -> Path:
    """Mescla os arquivos no manifesto existente (chave = chave do S3)."""
    entradas = {a.chave: a for a in ler_manifesto(destino)}
    entradas.update({a.chave: a for a in arquivos})
    caminho = destino / MANIFESTO
    dados = {
        "atualizado_em": datetime.now(timezone.utc).isoformat(),
        "arquivos": [entradas[k].para_dict() for k in sorted(entradas)],
    }
    caminho.write_text(
        json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return caminho


def ler_manifesto(destino: Path) -> list[ArquivoLegado]:
    caminho = destino / MANIFESTO
    if not caminho.exists():
        return []
    dados = json.loads(caminho.read_text(encoding="utf-8"))
    return [ArquivoLegado.de_dict(d) for d in dados["arquivos"]]


def escolher_por_periodo(arquivos: list[ArquivoLegado]) -> list[ArquivoLegado]:
    """Um arquivo por (dataset, período), na ordem de PREFERENCIA_FORMATO."""
    escolhidos: dict[tuple, ArquivoLegado] = {}
    for a in arquivos:
        chave = (a.dataset, a.periodo)
        atual = escolhidos.get(chave)
        if atual is None or PREFERENCIA_FORMATO.index(
            a.formato
        ) < PREFERENCIA_FORMATO.index(atual.formato):
            escolhidos[chave] = a
    return sorted(
        escolhidos.values(),
        key=lambda a: (a.dataset, a.periodo.ano, a.periodo.trimestre or 0),
    )
