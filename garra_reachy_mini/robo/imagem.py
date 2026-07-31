"""Leitura mínima de cabeçalho JPEG.

O SDK devolve JPEG pronto (`media.get_frame_jpeg()`) e não diz o tamanho. Dá para
descobrir lendo os marcadores SOF sem decodificar nada — 30 linhas contra uma
dependência de Pillow ou OpenCV (que nem está instalado neste venv).
"""

from __future__ import annotations

# SOF0..SOF15, menos os marcadores que não carregam dimensão (DHT, JPG, DAC).
_SOF = {
    0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
    0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
}


def dimensoes_jpeg(dados: bytes) -> tuple[int, int] | None:
    """(largura, altura) ou None se não for um JPEG legível."""
    if len(dados) < 4 or dados[0] != 0xFF or dados[1] != 0xD8:
        return None
    i = 2
    n = len(dados)
    while i + 3 < n:
        if dados[i] != 0xFF:
            i += 1  # ressincroniza em byte de preenchimento
            continue
        marcador = dados[i + 1]
        i += 2
        if marcador in (0xD8, 0xD9) or 0xD0 <= marcador <= 0xD7:
            continue  # marcadores sem payload
        if i + 1 >= n:
            return None
        tamanho = (dados[i] << 8) | dados[i + 1]
        if marcador in _SOF:
            if i + 6 >= n:
                return None
            altura = (dados[i + 3] << 8) | dados[i + 4]
            largura = (dados[i + 5] << 8) | dados[i + 6]
            return largura, altura
        if tamanho < 2:
            return None
        i += tamanho
    return None
