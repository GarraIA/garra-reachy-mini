"""Cliente do serviço de rostos (`tools/servidor_rostos.py`, 127.0.0.1:8127).

A ponte não carrega OpenCV nem toca em vetor: só repassa ao serviço e traduz
o que ele responde. Quando o serviço está fora, é um estado ("offline") e não
uma exceção sem nome — o painel mostra e diz como ligar.
"""

from __future__ import annotations

import httpx

SERVICO = "http://127.0.0.1:8127"
TIMEOUT_S = 10.0

# Códigos que vêm do serviço passam inteiros; estes três são desta camada.
CODIGOS_DAQUI = ("service_offline", "timeout", "service_error")


class ErroFamilia(Exception):
    def __init__(self, codigo: str, detalhe: str = "") -> None:
        super().__init__(f"{codigo}: {detalhe}" if detalhe else codigo)
        self.codigo = codigo
        self.detalhe = detalhe


_VAZIO = {"state": "offline", "engine": None, "profiles": None, "unreadable_profiles": None,
          "threshold": None, "margin": None, "max_samples": None, "error": None}


class Familia:
    def __init__(self, http: httpx.Client | None = None) -> None:
        # `trust_env=False`: um HTTP_PROXY no ambiente do companion mandaria a
        # imagem da câmera para o proxy em vez do loopback.
        self.http = http or httpx.Client(base_url=SERVICO, timeout=TIMEOUT_S, trust_env=False)

    def _pedir(self, metodo: str, caminho: str, json=None, jpeg: bytes | None = None) -> dict:
        try:
            if jpeg is not None:
                r = self.http.request(metodo, caminho, content=jpeg, headers={"Content-Type": "image/jpeg"})
            else:
                r = self.http.request(metodo, caminho, json=json)
        except httpx.TimeoutException:
            raise ErroFamilia("timeout", "o serviço de rostos demorou")
        except httpx.HTTPError as e:
            raise ErroFamilia("service_offline", type(e).__name__)
        try:
            corpo = r.json()
        except ValueError:
            raise ErroFamilia("service_error", f"resposta {r.status_code} sem JSON")
        if not isinstance(corpo, dict):
            raise ErroFamilia("service_error", "resposta inesperada")
        if not r.is_success:
            erro = corpo.get("error") if isinstance(corpo.get("error"), dict) else {}
            raise ErroFamilia(str(erro.get("code") or "service_error"), str(erro.get("message") or "")[:120])
        return corpo

    def estado(self) -> dict:
        """`offline` é serviço parado; `error` é serviço de pé com problema
        (base corrompida, resposta torta) — o painel diz coisas diferentes."""
        try:
            s = self._pedir("GET", "/saude")
        except ErroFamilia as e:
            if e.codigo == "service_offline":
                return dict(_VAZIO)
            return dict(_VAZIO, state="error", error=e.codigo)
        estado = {"state": "ok" if s.get("ok", True) else "error", "engine": s.get("engine"),
                  "profiles": s.get("profiles"), "unreadable_profiles": s.get("unreadable_profiles"),
                  "threshold": s.get("threshold"), "margin": s.get("margin"), "max_samples": s.get("max_samples"),
                  "error": None if s.get("ok", True) else str(s.get("error") or "service_error")}
        return estado

    def perfis(self) -> list[dict]:
        return list(self._pedir("GET", "/perfis").get("profiles") or [])

    def criar(self, nome: str, consentimento: dict) -> dict:
        return self._pedir("POST", "/perfis", json={"name": nome, "consent": consentimento})["profile"]

    def ativo(self, perfil_id: str, ativo: bool) -> dict:
        return self._pedir("PATCH", f"/perfis/{perfil_id}", json={"active": ativo})["profile"]

    def apagar(self, perfil_id: str) -> dict:
        return self._pedir("DELETE", f"/perfis/{perfil_id}")

    def limpar_amostras(self, perfil_id: str) -> dict:
        return self._pedir("DELETE", f"/perfis/{perfil_id}/amostras")

    def amostra(self, perfil_id: str, jpeg: bytes) -> dict:
        return self._pedir("POST", f"/perfis/{perfil_id}/amostras", jpeg=jpeg)

    def reconhecer(self, jpeg: bytes) -> dict:
        return self._pedir("POST", "/reconhecer", jpeg=jpeg)
