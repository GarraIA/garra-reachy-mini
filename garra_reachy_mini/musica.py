"""Música, do lado do robô: pedir à ferramenta do PC e dizer o que ela CONFERIU.

Quem entende a frase é `musica_pedido` (determinístico, sem modelo). Quem toca
é o companion, pelo spotatui do PC (`companion/musica_comandos.py`), que só
devolve "tocando" depois de ver a faixa pedida tocando no aparelho alvo com o
tempo andando. Aqui fica o resto:

- a frase falada sai do RESULTADO, nunca do pedido — o nome é o que o player
  diz que está tocando, e "está tocando" só com `status == "playing"`;
- falha vira uma frase curta sobre o problema real, sem código nem detalhe;
- o pedido de música não vai ao modelo, e nada da Spotify vai (Developer
  Policy III.14). O modelo só fica sabendo que a capacidade existe e como se
  pede, quando alguém fala de música sem dar um comando;
- o comando por voz é opt-in do dono (`music_voice_enabled`): a Developer
  Policy III.3 da Spotify não permite assistente de voz em app distribuído, e
  isso está dito no painel, onde se liga.
"""

from __future__ import annotations

import re
from typing import Any

import requests

from .robo.intencoes import normalizar

# Menor que o teto do laço de voz (main.MUSICA_PRAZO_S): o robô não fica surdo esperando.
TIMEOUT_S = 25.0
TIMEOUT_ABAFAR_S = 6.0

DESLIGADO = ("O comando de música por voz está desligado. Dá para ligar no painel do robô, "
             "em Controle › Música.")
SEM_COMPUTADOR = "Não consegui falar com o computador agora."
_GENERICA = "Não consegui fazer isso no Spotify agora."

_REMASTER = re.compile(
    r"\s+-\s+(?:\d{4}\s+)?(?:digital(?:ly)?\s+)?remaster(?:ed|izad[ao])?(?:\s+(?:version|\d{4}))*\s*$|"
    r"\s*\((?:\d{4}\s+)?(?:digital(?:ly)?\s+)?remaster(?:ed|izad[ao])?(?:\s+(?:version|\d{4}))*\)\s*$",
    re.I)
_FORA = re.compile(r"[\[\]\x00-\x1f\x7f]")


def _limpo(texto: Any) -> str:
    """Texto que veio do player vai para a fala e para o chat: sem colchete, sem
    quebra de linha — nada que pareça um bloco do envelope."""
    return re.sub(r"\s+", " ", _FORA.sub(" ", str(texto or ""))).strip()


def titulo_falado(nome: Any) -> str:
    """"Hotel California - 2013 Remaster" se fala "Hotel California"."""
    return _REMASTER.sub("", _limpo(nome)).strip()


def artistas_falados(artistas: Any) -> str:
    nomes = [_limpo(a).replace(" & ", " e ") for a in (artistas or []) if _limpo(a)]
    if len(nomes) <= 1:
        return nomes[0] if nomes else ""
    return ", ".join(nomes[:-1]) + " e " + nomes[-1]


def _musica(r: dict) -> str:
    faixa = r.get("track") or {}
    nome, quem = titulo_falado(faixa.get("name")), artistas_falados(faixa.get("artists"))
    if nome and quem:
        return f"{nome}, de {quem}"
    return nome or "a música"


_INDISPONIVEL = {
    "player_off": "O dispositivo de reprodução está indisponível: o player do Spotify no computador está fechado.",
    "auth_expired": "Preciso que você reconecte sua conta do Spotify no computador.",
    "unreachable": "Não consegui conectar ao Spotify agora.",
    "not_installed": "O Spotify não está configurado no computador.",
}
_NAO_CONFERIDO = {
    "pause": "Pedi para pausar, mas não consegui confirmar.",
    "resume": "Pedi para continuar, mas não consegui confirmar.",
    "next": "Pedi para pular, mas não consegui confirmar.",
    "previous": "Pedi para voltar, mas não consegui confirmar.",
    "set_volume": "Pedi para mudar o volume, mas não consegui confirmar.",
    "change_volume": "Pedi para mudar o volume, mas não consegui confirmar.",
}


def frase(r: dict | None) -> str:
    """O que o robô fala depois do resultado. Sucesso só com o estado conferido."""
    if r is None:
        return SEM_COMPUTADOR
    st, motivo, op = r.get("status"), r.get("reason"), r.get("operation")
    ja = bool(r.get("already"))
    if st == "unavailable":
        if motivo == "other_device":
            onde = _limpo((r.get("observed") or {}).get("device"))
            return (f"A música está tocando em outro aparelho{f' ({onde})' if onde else ''}, "
                    "e eu só controlo o player do computador.")
        return _INDISPONIVEL.get(motivo, "O Spotify está indisponível agora.")
    if st == "failed":
        if motivo == "not_verified":
            return _NAO_CONFERIDO.get(op, "Pedi ao Spotify, mas não consegui confirmar que a música começou.")
        if motivo == "timeout":
            return "O Spotify demorou demais para responder."
        return _GENERICA
    if st == "pending":
        return "O Spotify ainda está respondendo ao pedido anterior."
    if st == "idle":
        if op == "now_playing":
            return "Nada está tocando agora."
        if op == "queue_track":
            return "Nada está tocando agora. Peça para eu tocar uma música primeiro."
        return "Não há música tocando agora."
    if st == "not_found":
        if motivo == "no_search":
            return "Ainda não procurei nenhuma música. Peça, por exemplo: procura Hotel California."
        if op == "play_playlist":
            return "Não encontrei uma playlist sua com esse nome."
        return "Não encontrei essa música no Spotify."
    if st == "found":
        return f"Encontrei {_musica(r)}. Para ouvir, diga: toca essa."
    if st == "queued":
        return f"{_musica(r)}, já está na fila." if ja else f"Adicionei à fila: {_musica(r)}."
    if st == "volume_set":
        v = r.get("volume_percent")
        if ja and motivo == "at_limit":
            return "O volume já está no máximo." if (v or 0) >= 100 else "O volume já está no mínimo."
        return f"Volume em {v} por cento." if isinstance(v, int) else "Mudei o volume."
    if st == "paused":
        if op == "pause":
            return "A música já está pausada." if ja else "Pausei."
        if op == "next" and motivo == "no_next":
            return "Não tem próxima música na fila."
        if op == "next":
            return f"Pulei para {_musica(r)}, mas a música está pausada."
        if op == "previous":
            return f"Voltei para {_musica(r)}, mas a música está pausada."
        return f"Está pausada: {_musica(r)}."
    if st == "playing":
        if op == "now_playing":
            return f"Está tocando {_musica(r)}."
        if op == "resume":
            return "A música já está tocando." if ja else "Retomei a música."
        if op == "next":
            if motivo == "no_next":
                return "Não tem próxima música na fila; continuei a mesma."
            return f"Pulei. Agora está tocando {_musica(r)}."
        if op == "previous":
            return f"Voltei. Está tocando {_musica(r)}."
        if ja:
            return f"Já está tocando {_musica(r)}."
        if op == "play_generic" and r.get("resumed"):
            return f"Retomei {_musica(r)}."
        if op == "play_playlist":
            nome = _limpo((r.get("playlist") or {}).get("name"))
            if nome:
                return f"Pronto, está tocando a playlist {nome}."
        return f"Pronto, está tocando {_musica(r)}."
    return _GENERICA


def aviso(operacao: str) -> str:
    """A frase curta de quando o PC demora — nunca anuncia que tocou."""
    if operacao in ("play_track", "play_playlist", "queue_track", "search_track"):
        return "Vou procurar essa música."
    return "Só um instante."


def pedido_da_ferramenta(operacao: str, consulta: str | None, volume: int | None, request_id: str,
                         origem: str) -> dict[str, Any]:
    """O pedido tipado que vai ao companion (`POST /api/musica/comando`)."""
    pedido: dict[str, Any] = {"request_id": request_id, "source": origem, "operation": operacao}
    if consulta is not None:
        pedido["query"] = consulta
    if operacao == "set_volume":
        pedido["volume_percent"] = volume
    elif operacao == "change_volume":
        pedido["volume_delta"] = volume
    return pedido


# ── o que o modelo fica sabendo ──────────────────────────────────────────────
_FALA_DE_MUSICA = re.compile(r"\b(spotify|musica|musicas|playlist|playlists|faixa|cancao|album|cantor|cantora|"
                             r"banda|music|song|songs)\b")
# Com música no assunto, "volume", "som", "alto" também são sobre ela.
_NO_ASSUNTO = re.compile(r"\b(volume|som|alto|baixo|tocar|toca|tocando|pausa|proxima|anterior)\b")
# Com a música tocando e a frase de ativação perdida nela: sinal de que alguém
# tentou chamar o robô ("fala gata pausa a música", "ela agarra…").
_ATENCAO = re.compile(r"\b(garra|pausa|pause|pare|para|proxima|pula|volta|volume|abaixa|aumenta|diminui|musica|"
                      r"spotify|toca|som)\b")


def fala_de_musica(texto: str, no_assunto: bool = False) -> bool:
    """Falou de música sem dar um comando? Então o modelo precisa da situação —
    senão ele conclui "não controlo música" (medido em 30/09 com "aumento de volume")."""
    n = normalizar(texto or "")
    return bool(_FALA_DE_MUSICA.search(n)) or (no_assunto and bool(_NO_ASSUNTO.search(n)))


def pede_atencao(texto: str) -> bool:
    """Com a música tocando, o que foi ouvido sem a frase de ativação parece alguém chamando?"""
    return bool(_ATENCAO.search(normalizar(texto or "")))


def situacao(habilitada: bool) -> str:
    """Uma frase de fato do sistema para o modelo — sem nada vindo da Spotify."""
    if habilitada:
        return ("Música: o robô controla o Spotify do computador por comando de voz. Para tocar, a pessoa "
                "diz, por exemplo: 'Garra, toca Hotel California', 'toca uma música', 'pausa', 'próxima' "
                "ou 'que música está tocando?'. Se ela quiser isso, diga que é só pedir assim; não diga "
                "que não tem acesso ao Spotify.")
    return ("Música: o robô sabe controlar o Spotify do computador, mas o comando por voz está desligado "
            "no painel do robô, em Controle › Música. Diga isso, sem afirmar que o recurso não existe.")


def capacidade(habilitada: bool) -> str:
    """A resposta a "você consegue tocar música?" — sem ir ao modelo."""
    if habilitada:
        return ("Consigo, sim: toco no Spotify do computador. É só pedir, por exemplo: "
                "Garra, toca Hotel California.")
    return ("Consigo tocar no Spotify do computador, mas o comando por voz está desligado. "
            "Dá para ligar no painel do robô, em Controle › Música.")


# ── o cliente da ferramenta ──────────────────────────────────────────────────
class ClienteMusica:
    """`POST /api/musica/*` da ponte, com o token da conversa."""

    def __init__(self, base_url: str, token: str | None, timeout_s: float = TIMEOUT_S) -> None:
        self.base = (base_url or "").rstrip("/")
        self._cab = {"Authorization": f"Bearer {token}"} if token else {}
        self.timeout_s = timeout_s
        self.sessao = requests.Session()
        self.sessao.trust_env = False

    def _post(self, caminho: str, corpo: dict, timeout_s: float) -> dict | None:
        r = self.sessao.post(f"{self.base}{caminho}", json=corpo, headers=self._cab, timeout=timeout_s,
                             allow_redirects=False)
        dados = r.json()
        return dados if r.status_code == 200 and isinstance(dados, dict) else None

    def comando(self, pedido: dict) -> dict | None:
        """O resultado conferido, ou `None` quando o PC não respondeu.

        Uma reentrega só, com o MESMO `request_id`: se a primeira chegou e a
        resposta se perdeu, o PC devolve o resultado dela sem executar de novo.
        """
        if not self.base:
            return None
        for tentativa in (1, 2):
            try:
                return self._post("/api/musica/comando", pedido, self.timeout_s)
            except requests.Timeout:
                # O PC está executando: reenviar só dobraria a espera.
                return None
            except requests.ConnectionError:
                if tentativa == 2:
                    return None
            except (requests.RequestException, ValueError):
                return None
        return None

    def abafar(self, request_id: str) -> dict | None:
        return self._sem_erro("/api/musica/abafar", request_id)

    def restaurar(self, request_id: str) -> dict | None:
        return self._sem_erro("/api/musica/restaurar", request_id)

    def _sem_erro(self, caminho: str, request_id: str) -> dict | None:
        """Abaixar a música é cortesia: se falhar, a fala sai assim mesmo."""
        if not self.base:
            return None
        try:
            return self._post(caminho, {"request_id": request_id}, TIMEOUT_ABAFAR_S)
        except (requests.RequestException, ValueError):
            return None


__all__ = ["ClienteMusica", "DESLIGADO", "SEM_COMPUTADOR", "artistas_falados", "aviso", "capacidade",
           "fala_de_musica", "frase", "pede_atencao", "pedido_da_ferramenta", "situacao", "titulo_falado"]
