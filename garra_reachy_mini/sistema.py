"""O que o painel mostra sobre o robô em si: versões, áudio e atualização.

Tudo vem do daemon oficial do Reachy Mini, pela API dele. Este módulo só LÊ —
com duas exceções, o volume do alto-falante e o ganho do microfone, que são
números de 0 a 100 e passam pelas rotas do próprio daemon.

Atualizar o robô não é daqui. A rota confere se há versão nova (é o daemon
quem pergunta ao índice de pacotes) e diz como atualizar; instalar é com o app
oficial Reachy Mini Control ou com o script `atualizar_robo.py`, que validam o
resultado e sabem voltar atrás.
"""

from __future__ import annotations

import time
from typing import Any

import requests

TIMEOUT_S = 4.0
# Perguntar ao índice de pacotes custa uma ida à internet a partir do robô:
# no máximo uma vez a cada tanto, e só quando alguém pede.
ATUALIZACAO_VALE_S = 600.0


class Sistema:
    def __init__(self, daemon_url: str) -> None:
        self.base = daemon_url.rstrip("/")
        self._atualizacao: tuple[float, dict[str, Any]] | None = None

    def _ler(self, caminho: str, timeout: float = TIMEOUT_S) -> Any:
        r = requests.get(f"{self.base}{caminho}", timeout=timeout)
        r.raise_for_status()
        return r.json()

    def resumo(self) -> dict[str, Any]:
        """Versões e áudio. Cada parte falha sozinha: `None` é "não sei"."""
        saida: dict[str, Any] = {"daemon": None, "audio": None, "update": None}
        try:
            d = self._ler("/api/daemon/status")
            saida["daemon"] = {
                "version": d.get("version"), "state": d.get("state"),
                "wireless": d.get("wireless_version"), "ip": d.get("wlan_ip"),
                "robot_name": d.get("robot_name"),
                "control_loop_hz": round(float(((d.get("backend_status") or {})
                                                .get("control_loop_stats") or {})
                                               .get("mean_control_loop_frequency") or 0), 1),
            }
        except (requests.RequestException, ValueError, TypeError):
            pass
        try:
            fala, mic = self._ler("/api/volume/current"), self._ler("/api/volume/microphone/current")
            saida["audio"] = {"output_device": fala.get("device"),
                              "input_device": mic.get("device"),
                              "speaker_volume": fala.get("volume"),
                              "microphone_volume": mic.get("volume")}
        except (requests.RequestException, ValueError, AttributeError):
            pass
        if self._atualizacao is not None:
            saida["update"] = {**self._atualizacao[1],
                               "checked_at": self._atualizacao[0]}
        return saida

    def conferir_atualizacao(self) -> dict[str, Any]:
        """Há versão nova do software do robô? Só consulta; não instala nada."""
        agora = time.time()
        if self._atualizacao and agora - self._atualizacao[0] < ATUALIZACAO_VALE_S:
            return {**self._atualizacao[1], "checked_at": self._atualizacao[0]}
        dados = self._ler("/update/available", timeout=20.0)
        pacote = ((dados.get("update") or {}).get("reachy_mini") or {}) \
            if isinstance(dados, dict) else {}
        resultado = {"available": bool(pacote.get("is_available")),
                     "current_version": pacote.get("current_version"),
                     "available_version": pacote.get("available_version")}
        self._atualizacao = (agora, resultado)
        return {**resultado, "checked_at": agora}

    def definir_volume(self, *, alto_falante: int | None = None,
                       microfone: int | None = None) -> dict[str, Any]:
        for caminho, valor in (("/api/volume/set", alto_falante),
                               ("/api/volume/microphone/set", microfone)):
            if valor is None:
                continue
            r = requests.post(f"{self.base}{caminho}", json={"volume": int(valor)},
                              timeout=TIMEOUT_S)
            r.raise_for_status()
        return self.resumo()["audio"] or {}

    def som_de_teste(self) -> None:
        r = requests.post(f"{self.base}/api/volume/test-sound", timeout=TIMEOUT_S)
        r.raise_for_status()
