"""Quais subsistemas estão de pé, e o que fazer com os que não estão.

Este app nasceu ligado à infraestrutura pessoal do autor: um gateway do Garra
para pensar e um servidor de voz com GPU para ouvir e falar. Instalado da loja,
num robô de outra pessoa, nada disso existe — e a versão antiga simplesmente
travava esperando o servidor de voz aparecer.

A regra agora é: **nenhum serviço opcional pode impedir o robô de funcionar.**
Câmera, movimento, expressões, danças e rastreamento de rosto dependem só do
robô. Voz e conversa entram quando (e se) forem configurados. O que este módulo
faz é registrar esse estado num lugar só, para o painel e a API dizerem em voz
clara o que falta em vez de dar a impressão de que o app está quebrado.

Os códigos de motivo são estáveis e em inglês porque atravessam a API pública;
quem traduz para o usuário é o `static/i18n.js`.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .robo.barramento import Barramento

# Serviços essenciais: sem eles o app não tem sentido nenhum.
ESSENCIAIS = frozenset({"robot"})


@dataclass(frozen=True)
class Servico:
    nome: str
    disponivel: bool = False
    codigo: str = "starting"
    detalhe: str = ""
    dica: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def essencial(self) -> bool:
        return self.nome in ESSENCIAIS

    def json(self) -> dict:
        d = {
            "name": self.nome,
            "available": self.disponivel,
            "required": self.essencial,
            "reason_code": self.codigo,
        }
        if self.detalhe:
            d["detail"] = self.detalhe
        if self.dica:
            d["hint"] = self.dica
        if self.extra:
            d["extra"] = dict(self.extra)
        return d


class Servicos:
    """Registro observável do estado de cada subsistema.

    `marcar` só publica evento quando algo muda de verdade — o supervisor chama
    isto a cada rodada de reprovação da voz, e um evento a cada 5 s por um
    estado que não mudou entupiria o WebSocket do painel à toa.
    """

    NOMES = ("robot", "movement", "camera", "voice", "gateway", "brain")

    def __init__(self, barramento: "Barramento | None" = None) -> None:
        self._lock = threading.Lock()
        self._barramento = barramento
        self._itens: dict[str, Servico] = {n: Servico(n) for n in self.NOMES}

    def conectar(self, barramento: "Barramento") -> None:
        """Liga ao barramento de eventos, que só existe depois do robô."""
        with self._lock:
            self._barramento = barramento

    def marcar(
        self,
        nome: str,
        disponivel: bool,
        *,
        codigo: str = "",
        detalhe: str = "",
        dica: str = "",
        **extra: object,
    ) -> None:
        novo = Servico(
            nome=nome,
            disponivel=disponivel,
            codigo=codigo or ("ok" if disponivel else "unavailable"),
            detalhe=detalhe,
            dica=dica,
            extra=dict(extra),
        )
        with self._lock:
            if self._itens.get(nome) == novo:
                return
            self._itens[nome] = novo
            instantaneo = self._json_sem_lock()
            barramento = self._barramento
        # Fora do lock de propósito: publicar não pode bloquear quem marca.
        if barramento is not None:
            barramento.publicar("robot.services", **instantaneo)

    def obter(self, nome: str) -> Servico:
        with self._lock:
            return self._itens.get(nome, Servico(nome))

    def _json_sem_lock(self) -> dict:
        itens = [s.json() for s in self._itens.values()]
        faltando = [s["name"] for s in itens if not s["available"]]
        return {
            "services": itens,
            "limited": bool(faltando),
            "missing": faltando,
        }

    def json(self) -> dict:
        with self._lock:
            return self._json_sem_lock()
