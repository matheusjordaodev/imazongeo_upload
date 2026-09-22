"""Correções automáticas aplicadas aos envios, antes da gravação.

Cada correção é contada e informada nos avisos do envio:

- textos com codificação quebrada (UTF-8 lido como Latin-1/CP1252/CP850, às
  vezes várias vezes, ex.: ``CARAJÃ\\x83Â\\x81S`` → ``CARAJÁS``) e espaços
  sobrando;
- valores de vocabulário conhecido com grafia diferente (acentos, maiúsculas
  ou letra perdida ``�``), ex.: ``Terra Indigena`` → ``Terra Indígena``;
- área em hectares calculada da geometria (elipsoide WGS 84);
- registros idênticos repetidos no mesmo envio.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

from pyproj import Geod

PERDIDO = "�"  # caractere de substituição: a letra original se perdeu
_MARCAS = re.compile("[\u0080-¿ÂÃâƒ‘-™├┬┼║╗╝═]")
_GEOD = Geod(ellps="WGS84")


class Contador(Counter):
    """Quantidade de correções por tipo (vira aviso do envio)."""

    def avisos(self, nome: str) -> list[str]:
        return [f"{nome}: {n} {tipo}" for tipo, n in sorted(self.items()) if n]


# ---------------------------------------------------------------------------
# Texto
# ---------------------------------------------------------------------------


def _para_bytes(texto: str, codec: str) -> bytes | None:
    """Bytes originais de um texto UTF-8 decodificado errado com ``codec``.

    Caracteres fora do codec (ex.: controles C1 que o CP1252 não define) são
    tratados como Latin-1, como fazem os programas que geraram o erro.
    """
    saida = bytearray()
    for c in texto:
        try:
            saida += c.encode(codec)
        except UnicodeEncodeError:
            if ord(c) > 0xFF:
                return None
            saida.append(ord(c))
    return bytes(saida)


def corrigir_codificacao(texto: str) -> str:
    """Desfaz a leitura de UTF-8 como Latin-1/CP1252/CP850, quantas vezes houver.

    Só altera o texto se o resultado for UTF-8 válido: nomes corretos com
    acento (ex.: ``TRAIRÃO``) não formam UTF-8 e ficam como estão.
    """
    for _ in range(4):
        if not _MARCAS.search(texto):
            break
        for codec in ("cp1252", "cp850"):
            dados = _para_bytes(texto, codec)
            if dados is None:
                continue
            try:
                novo = dados.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if novo != texto:
                texto = novo
                break
        else:
            break
    return texto


def limpar_texto(texto: str | None, contador: Contador | None = None) -> str | None:
    """Corrige a codificação e remove espaços sobrando (None se ficar vazio)."""
    if texto is None:
        return None
    corrigido = corrigir_codificacao(texto)
    if contador is not None and corrigido != texto:
        contador["texto(s) com codificação corrigida"] += 1
    limpo = " ".join(corrigido.split())
    if contador is not None and limpo != corrigido:
        contador["texto(s) com espaços corrigidos"] += 1
    return limpo or None


def chave_comparacao(texto: str) -> str:
    """Texto sem acentos, em minúsculas e com espaços simples."""
    sem_acento = unicodedata.normalize("NFKD", texto)
    sem_acento = "".join(c for c in sem_acento if not unicodedata.combining(c))
    return " ".join(sem_acento.lower().split())


def _casa(texto: str, candidato: str) -> bool:
    """``texto`` é ``candidato`` com outra grafia (``�`` vale qualquer letra)?"""
    a, b = chave_comparacao(texto), chave_comparacao(candidato)
    if PERDIDO not in texto:
        return a == b
    padrao = re.escape(a).replace(re.escape(PERDIDO), ".")
    return re.fullmatch(padrao, b) is not None


def padronizar(
    valor: str | None, vocabulario: tuple[str, ...], contador: Contador | None = None
) -> str | None:
    """Grafia do vocabulário para ``valor`` (acentos, caixa, ``�``).

    Valores fora do vocabulário ficam como estão.
    """
    if valor is None or valor in vocabulario:
        return valor
    candidatos = [v for v in vocabulario if _casa(valor, v)]
    if len(candidatos) == 1:
        if contador is not None:
            contador["valor(es) com grafia padronizada"] += 1
        return candidatos[0]
    return valor


def padronizar_palavras(
    texto: str | None, palavras: tuple[str, ...], contador: Contador | None = None
) -> str | None:
    """Aplica :func:`padronizar` a cada palavra (ex.: meses na legenda do A&P)."""
    if texto is None:
        return None
    novo = " ".join(padronizar(p, palavras) or p for p in texto.split(" "))
    if contador is not None and novo != texto:
        contador["valor(es) com grafia padronizada"] += 1
    return novo


def nome_conhecido(nome: str, conhecidos: list[str]) -> str | None:
    """Nome correto para um nome com ``�``, se houver exatamente um que case."""
    if PERDIDO not in nome:
        return None
    candidatos = {c for c in conhecidos if PERDIDO not in c and _casa(nome, c)}
    return candidatos.pop() if len(candidatos) == 1 else None


# ---------------------------------------------------------------------------
# Vocabulários
# ---------------------------------------------------------------------------

MESES = (
    "janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
    "agosto", "setembro", "outubro", "novembro", "dezembro",
)  # fmt: skip

USOS_AP = ("Uso Sustentável", "Proteção Integral", "Terra Indígena")
JURISDICOES_AP = ("Federal", "Estadual")
# Categorias do SNUC (Lei 9.985/2000) e Terra Indígena
CATEGORIAS_AP = (
    "Terra Indígena",
    "Estação Ecológica",
    "Reserva Biológica",
    "Parque Nacional",
    "Parque Estadual",
    "Monumento Natural",
    "Refúgio de Vida Silvestre",
    "Área de Proteção Ambiental",
    "Área de Relevante Interesse Ecológico",
    "Floresta Nacional",
    "Floresta Estadual",
    "Floresta Extrativista",
    "Reserva Extrativista",
    "Reserva de Fauna",
    "Reserva de Desenvolvimento Sustentável",
    "Reserva Particular do Patrimônio Natural",
)
ESTADOS = (
    "Acre", "Amapá", "Amazonas", "Maranhão", "Mato Grosso", "Pará",
    "Rondônia", "Roraima", "Tocantins",
)  # fmt: skip


# ---------------------------------------------------------------------------
# Área e duplicatas
# ---------------------------------------------------------------------------


def area_ha(geometria) -> float:
    """Área geodésica (elipsoide WGS 84) em hectares, descontando os buracos."""
    area, _ = _GEOD.geometry_area_perimeter(geometria)
    return abs(area) / 1e4


def indices_repetidos(chaves: list) -> list[int]:
    """Índices de registros cuja chave já apareceu antes (fica o primeiro)."""
    vistos: set = set()
    repetidos = []
    for i, chave in enumerate(chaves):
        if chave in vistos:
            repetidos.append(i)
        else:
            vistos.add(chave)
    return repetidos
