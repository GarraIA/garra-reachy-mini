"""A casa, do lado do robô: o que dizer ao modelo quando o pedido é sobre aparelhos.

O robô não controla aparelho nenhum — quem controla é o Garra, pelo registro de
aparelhos e pelas portas de risco dele (`device_read`/`device_execute`). Mas o
modelo, no modo de voz, não enxerga essas ferramentas enquanto a casa não está
conectada ou a voz não foi liberada, e sem mais nada concluía "não tenho essa
integração" (medido no robô em 30/09). A verdade tem três camadas — existe no
Garra; falta o Home Assistant; a voz é opt-in — e é ela que vai no pedido.

Só pedidos sobre aparelhos puxam a situação (uma consulta leve à ponte). Com a
casa pronta e a voz liberada não há bloco: as ferramentas falam por si.
"""

from __future__ import annotations

import re
from typing import Any

import requests

from .robo.intencoes import normalizar

TIMEOUT_S = 4.0

# Depois de normalizado (sem acento, minúsculas, sem pontuação). Verbo de
# acionar + aparelho, pergunta sobre o estado de um aparelho, ou "controlar a
# casa". "ar" e "tv" só depois de um verbo de ligar/desligar: soltos, são
# palavras demais.
_APARELHO = (r"(luz|luzes|lampada|lampadas|abajur|tomada|tomadas|interruptor|ar condicionado|"
             r"ventilador|cortina|cortinas|persiana|persianas|termostato|aquecedor|fechadura|portao|porta|"
             r"garagem|televisao|aparelhos? da casa|dispositivos? da casa|light|lights|lamp|lamps|plug|fan|"
             r"thermostat|blinds|curtains|garage|door|tv)")
_VERBO = (r"(acende|acenda|acender|apaga|apague|apagar|liga|ligue|ligar|desliga|desligue|desligar|"
          r"abre|abra|abrir|fecha|feche|fechar|tranca|tranque|trancar|destranca|destrancar|"
          r"aumenta|diminui|ajusta|mexer|mexe)")
_GATILHOS = [
    re.compile(r"\b" + _VERBO + r"\b.{0,30}\b" + _APARELHO + r"\b"),
    re.compile(r"\b(liga|ligue|ligar|desliga|desligue|desligar) (o |a )?(ar|tv)\b"),
    re.compile(r"\b" + _APARELHO + r"\b.{0,30}\b(ligad|desligad|acesa|acesas|aceso|apagad|abert|fechad|trancad)"),
    re.compile(r"\b(temperatura|umidade) d[oa] (termostato|ar condicionado|casa|sala|quarto)\b"),
    re.compile(r"\b(controlar|controla|comandar|comanda|mexer) (n?[oa]s? )?(minha |nossa )?"
               r"(aparelhos|dispositivos|luzes|casa)\b"),
    re.compile(r"\b(casa inteligente|smart home|automacao da casa|automacao residencial)\b"),
    re.compile(r"\bturn (on|off) (the )?.{0,20}\b(light|lights|lamp|plug|fan|tv)\b"),
    re.compile(r"\bturn (the )?.{0,20}\b(light|lights|lamp|plug|fan|tv) (on|off)\b"),
    re.compile(r"\b(open|close) (the )?.{0,15}\b(blinds|curtains|garage|door)\b"),
    re.compile(r"\bset (the )?thermostat\b"),
    re.compile(r"\bis (the )?.{0,15}\b(light|lamp|fan|tv) (on|off)\b"),
]
# Lembrete, anotação e telefonema não são pedido para a casa agora: são do
# caderno ou de outra coisa.
_NAO_E_CASA = re.compile(r"\b(lembra|lembrar|lembre|lembrete|anota|anote|anotar|esquece|esquecer|"
                         r"lista|caderno|liga pra|liga para|ligar pra|ligar para|tomada de decisao)\b")


def pede_casa(texto: str) -> bool:
    """O pedido é sobre acionar ou consultar um aparelho da casa?"""
    n = normalizar(texto or "")
    return bool(n) and not _NAO_E_CASA.search(n) and any(g.search(n) for g in _GATILHOS)


def situacao(estado: dict[str, Any] | None) -> str | None:
    """Uma frase de fato do sistema para o modelo — ou None quando não precisa."""
    if not isinstance(estado, dict):
        return ("Controle da casa: não foi possível conferir a situação agora. Diga que não "
                "conseguiu verificar, e não que o recurso não existe.")
    st = estado.get("state")
    if st == "not_configured":
        return ("Controle da casa: o Garra tem o controle da casa, mas nenhum aparelho está "
                "registrado porque o Home Assistant ainda não foi conectado — isso se faz no "
                "painel do robô, em Controle › Casa. Diga que ainda não está configurado, e não "
                "que o recurso não existe.")
    if st == "pending":
        return ("Controle da casa: o Home Assistant está configurado, mas o Garra ainda não listou "
                "os aparelhos. Diga que está terminando de conectar.")
    if st == "broken":
        return ("Controle da casa: o Home Assistant está configurado, mas não está respondendo "
                "agora. Diga que o controle da casa está indisponível no momento.")
    if st == "unknown":
        voz = "" if estado.get("voice_control") is True else " O comando por voz está desligado no painel."
        return ("Controle da casa: o Home Assistant está configurado, mas não deu para confirmar agora "
                "se está respondendo." + voz + " Diga isso, sem afirmar que o recurso não existe.")
    if st == "ok" and estado.get("voice_control") is not True:
        return ("Controle da casa: está conectado, mas o comando de aparelhos está desligado por "
                "voz — quem liga é o dono, no painel do robô, em Controle › Casa. Diga isso.")
    return None


class ClienteCasa:
    """`GET /api/casa/estado` da ponte, com o token da conversa."""

    def __init__(self, base_url: str, token: str | None, timeout_s: float = TIMEOUT_S) -> None:
        self.base = (base_url or "").rstrip("/")
        self._cab = {"Authorization": f"Bearer {token}"} if token else {}
        self.timeout_s = timeout_s
        self.sessao = requests.Session()
        self.sessao.trust_env = False

    def estado(self) -> dict[str, Any] | None:
        if not self.base:
            return None
        try:
            # Rápido: sem sondar o Home Assistant — o turno de voz não espera sonda.
            r = self.sessao.get(f"{self.base}/api/casa/estado", params={"rapido": "1"}, headers=self._cab,
                                timeout=self.timeout_s, allow_redirects=False)
            dados = r.json()
        except (requests.RequestException, ValueError):
            return None
        if r.status_code != 200 or not isinstance(dados, dict):
            return None
        return dados


__all__ = ["ClienteCasa", "pede_casa", "situacao"]
