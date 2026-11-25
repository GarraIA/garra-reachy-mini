"""A trava do PC sobre a configuração do robô.

Dentro do robô o painel escuta na rede sem pedir senha (ver `seguranca.py`: lá
um token nosso não protegeria o robô, que o daemon já deixa aberto). Só que
`POST /api/config` decide PARA ONDE o robô manda coisas: o endereço do servidor
de voz recebe cada trecho de fala e a credencial; o da ponte recebe a conversa.
Sem trava, qualquer aparelho do Wi-Fi gravava um endereço seu e passava a
receber os dois.

Com `trava_pc` ligada — é o companion quem liga, ao parear — esses campos só
mudam com a assinatura de quem conhece a credencial que o robô já guarda. O
segredo nunca viaja: o robô emite um desafio de uso único, o PC devolve um HMAC
do desafio e do conteúdo pedido. Sem relógio, que um Raspberry sem bateria não
tem de onde tirar.

Do próprio robô (loopback) não se pede assinatura: é a saída de quem perdeu a
credencial do PC — entrar por SSH e desligar a trava ali.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import secrets
import threading
import time
from typing import Callable

# O que decide para onde o robô fala, e a própria trava.
PROTEGIDOS = ("gateway_url", "voz_url", "gateway_key", "voz_token", "garra_bin", "trava_pc",
              "hera_token", "hera_device")
# As credenciais que provam ser o PC pareado. Hoje carregam o mesmo valor.
CREDENCIAIS = ("voz_token", "gateway_key")
# O que a tela recebe mascarado e pode devolver mascarado sem que isso seja mudança.
MASCARADOS = CREDENCIAIS + ("hera_token",)
CABECALHO = "X-Garra-Assinatura"
VERSAO = "v1"


class Desafios:
    """Desafios de uso único, com prazo e teto: quem pede mil não enche a memória."""

    def __init__(self, validade_s: float = 60.0, maximo: int = 32,
                 relogio: Callable[[], float] = time.monotonic) -> None:
        self.validade_s = validade_s
        self._maximo = maximo
        self._relogio = relogio
        self._vivos: dict[str, float] = {}
        self._lock = threading.Lock()

    def emitir(self) -> str:
        nonce = secrets.token_urlsafe(18)
        with self._lock:
            self._vivos[nonce] = self._relogio() + self.validade_s
            while len(self._vivos) > self._maximo:
                self._vivos.pop(next(iter(self._vivos)))
        return nonce

    def consumir(self, nonce: str) -> bool:
        with self._lock:
            vence = self._vivos.pop(nonce, None)
        return vence is not None and self._relogio() <= vence


def assinatura(segredo: str, nonce: str, valores: dict) -> str:
    """HMAC do desafio e do pedido. `companion/reachy.py` tem a gêmea; um teste as compara."""
    corpo = json.dumps(valores, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    mensagem = f"garra-config-{VERSAO}\n{nonce}\n{corpo}".encode("utf-8")
    return hmac.new(segredo.encode("utf-8"), mensagem, hashlib.sha256).hexdigest()


def loopback(host: str | None) -> bool:
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def mudancas_protegidas(salva: dict, pedido: dict, mascara: str) -> list[str]:
    """Os campos protegidos que o pedido de fato mudaria (repetir o valor não conta)."""
    mudam = []
    for campo in PROTEGIDOS:
        if campo not in pedido:
            continue
        novo = pedido[campo]
        if novo == mascara and campo in MASCARADOS:
            continue                                  # a tela devolveu a máscara
        if (novo or None) != (salva.get(campo) or None):
            mudam.append(campo)
    return mudam


def assinada(salva: dict, pedido: dict, cabecalho: str | None, desafios: Desafios) -> bool:
    """O cabeçalho traz um desafio vivo e o HMAC certo sob uma credencial salva?"""
    partes = (cabecalho or "").split()
    if len(partes) != 3 or partes[0] != VERSAO:
        return False
    _, nonce, marca = partes
    if not desafios.consumir(nonce):
        return False
    return any(
        hmac.compare_digest(assinatura(str(salva[c]), nonce, pedido), marca)
        for c in CREDENCIAIS if salva.get(c))


def recusa(salva: dict, pedido: dict, cabecalho: str | None, desafios: Desafios,
           cliente: str | None, mascara: str) -> list[str]:
    """Os campos que barram este pedido — vazio quando ele pode ser gravado."""
    if not salva.get("trava_pc") or loopback(cliente):
        return []
    mudam = mudancas_protegidas(salva, pedido, mascara)
    if not mudam or assinada(salva, pedido, cabecalho, desafios):
        return []
    return mudam
