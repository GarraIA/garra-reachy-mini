"""Ritmo da conversa: quando (e se) o robô diz "só um instante".

Até a 1.0.1 o app tocava uma de quatro frases de espera **antes de toda
pergunta**, incondicionalmente. Numa tarefa longa isso ajuda; numa conversa
simples, atrasa a resposta e soa artificial — a frase saía mesmo quando o modelo
já ia responder em meio segundo.

O desenho aqui separa três coisas que estavam grudadas:

* **estado interno** (`thinking`) — continua sempre, e é visual: antenas, evento
  no barramento, pílula no painel. Não faz som.
* **aviso falado** — vira um evento *agendado e cancelável*, não uma
  consequência automática de perguntar.
* **resposta final** — começa a ser calculada **imediatamente**, em paralelo ao
  agendamento. O aviso nunca atrasa o processamento.

Duas armadilhas que este módulo existe para evitar:

1. **Prazos absolutos.** O aviso sai em `t0 + ack`, o progresso em
   `t0 + progresso` — e não `progresso` segundos *depois* do aviso, que daria
   `ack + progresso` e faria um aviso de 10 s virar 14 s.
2. **Um dono só do alto-falante.** `push_audio_sample` e `clear_player` são
   chamados exclusivamente pelo `CoordenadorAudio`. O lock protege a *transição
   de estado*, nunca fica preso durante a reprodução física — se ficasse, a
   resposta final não conseguiria cortar o aviso e o resultado seria deadlock.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable

MODOS = ("fast", "informative")

# Perfis por modo. Existem para que trocar de modo não apague, em silêncio, um
# tempo que o usuário ajustou à mão: cada modo guarda o seu.
PADRAO: dict[str, Any] = {
    "mode": "fast",
    # ── controle mestre ──────────────────────────────────────────────────────
    # Desligado, o robô só fala a resposta final e o que o usuário pediu
    # explicitamente. Nenhum atraso o contorna: 4 s, 30 s ou 5 min de espera
    # continuam em silêncio. Os controles abaixo são refinamentos DENTRO do
    # que este permite — nunca o contrário.
    #
    # `mode` (rápido/informativo) decide *quando* o aviso pode começar. Nunca
    # decidiu *se* ele existe, e era isso que faltava: em qualquer modo havia um
    # atraso que fazia a frase sair. Este par de chaves é o "se".
    "automatic_speech_enabled": True,
    "spoken_acknowledgements_enabled": True,
    "spoken_progress_updates": True,
    # Nenhum caminho de código anuncia ferramenta hoje — nada fala antes de uma
    # tool. A chave existe para que, se um preâmbulo aparecer, ele nasça
    # desligado e passe por aqui, em vez de virar mais uma fala automática sem
    # interruptor. Ver `voice.turn.tool_preamble`.
    "announce_tool_usage": False,
    "progress_update_delay_ms": 10000,
    "max_progress_messages": 1,
    "acknowledgement_cut_threshold_ms": 1200,
    "profiles": {
        # Rápido: só avisa se a espera passar de 4 s.
        "fast": {"acknowledgement_delay_ms": 4000},
        # Informativo: avisa cedo, a partir de 1,5 s.
        "informative": {"acknowledgement_delay_ms": 1500},
    },
    "revision": 0,
}

# Fala do arranque. Fica fora de `conversation` porque não pertence a turno
# nenhum: sai uma vez, sem pergunta, quando o loop de voz sobe.
PADRAO_ARRANQUE: dict[str, Any] = {"spoken_greeting_enabled": True}

BOOLEANOS = ("automatic_speech_enabled", "spoken_acknowledgements_enabled",
             "spoken_progress_updates", "announce_tool_usage")

LIMITES = {
    "progress_update_delay_ms": (1000, 120000),
    "max_progress_messages": (0, 5),
    "acknowledgement_cut_threshold_ms": (0, 10000),
    "acknowledgement_delay_ms": (0, 60000),
}

# Sem duração conhecida do áudio, esperamos no máximo isto antes de seguir.
TETO_DESCONHECIDO_MS = 2000

# Estados do aviso falado, na ordem em que acontecem.
AGENDADO, TOCANDO, CONCLUIDO, CANCELADO, CORTADO = (
    "scheduled", "playing", "completed", "cancelled", "flushed")
# Fora da ordem: o aviso nunca foi agendado, porque está desligado. Distinto de
# `cancelled` (chegou a ser agendado e a resposta venceu) e de `none` (não havia
# frase para tocar). Quem lê as métricas precisa separar "não deu tempo" de
# "o usuário desligou".
DESABILITADO = "disabled"


def normalizar(bruto: dict | None) -> dict:
    """Mescla o que veio do disco com o padrão, validando faixas.

    Tolerante de propósito: um `config.json` de uma versão anterior, ou editado
    à mão com um número absurdo, não pode impedir o robô de conversar.
    """
    c = {**PADRAO, "profiles": {k: dict(v) for k, v in PADRAO["profiles"].items()}}
    if not isinstance(bruto, dict):
        return c

    modo = str(bruto.get("mode") or "").strip().lower()
    if modo in MODOS:
        c["mode"] = modo
    for chave in BOOLEANOS:
        if chave in bruto:
            c[chave] = bool(bruto[chave])
    for campo in ("progress_update_delay_ms", "max_progress_messages",
                  "acknowledgement_cut_threshold_ms"):
        if campo in bruto:
            c[campo] = _inteiro(bruto[campo], c[campo], *LIMITES[campo])
    perfis = bruto.get("profiles")
    if isinstance(perfis, dict):
        for nome in MODOS:
            p = perfis.get(nome)
            if isinstance(p, dict) and "acknowledgement_delay_ms" in p:
                c["profiles"][nome]["acknowledgement_delay_ms"] = _inteiro(
                    p["acknowledgement_delay_ms"],
                    c["profiles"][nome]["acknowledgement_delay_ms"],
                    *LIMITES["acknowledgement_delay_ms"])
    c["revision"] = _inteiro(bruto.get("revision"), 0, 0, 2**31)
    return c


def normalizar_arranque(bruto: dict | None) -> dict:
    """O bloco `startup`, com a mesma tolerância do `conversation`."""
    c = dict(PADRAO_ARRANQUE)
    if isinstance(bruto, dict) and "spoken_greeting_enabled" in bruto:
        c["spoken_greeting_enabled"] = bool(bruto["spoken_greeting_enabled"])
    return c


def _inteiro(valor: Any, padrao: int, minimo: int, maximo: int) -> int:
    try:
        return max(minimo, min(maximo, int(valor)))
    except (TypeError, ValueError):
        return padrao


@dataclass(frozen=True)
class Politica:
    """Os tempos já resolvidos para o modo em vigor."""

    modo: str
    ack_atraso_ms: int
    progresso_atraso_ms: int
    max_progresso: int
    corte_ms: int
    progresso_falado: bool
    fala_automatica: bool = True
    ack_habilitado: bool = True
    anunciar_ferramenta: bool = False
    saudacao_habilitada: bool = True

    @classmethod
    def de(cls, conf: dict, arranque: dict | None = None) -> "Politica":
        c = normalizar(conf)
        a = normalizar_arranque(arranque)
        perfil = c["profiles"][c["mode"]]
        return cls(
            modo=c["mode"],
            ack_atraso_ms=perfil["acknowledgement_delay_ms"],
            progresso_atraso_ms=c["progress_update_delay_ms"],
            max_progresso=c["max_progress_messages"],
            corte_ms=c["acknowledgement_cut_threshold_ms"],
            progresso_falado=c["spoken_progress_updates"],
            fala_automatica=c["automatic_speech_enabled"],
            ack_habilitado=c["spoken_acknowledgements_enabled"],
            anunciar_ferramenta=c["announce_tool_usage"],
            saudacao_habilitada=a["spoken_greeting_enabled"],
        )

    # ── o que pode sair pelo alto-falante sem o usuário pedir ────────────────
    # Quatro perguntas, um lugar só. O mestre entra em cada uma por `and`, e é
    # por isso que nenhum atraso, modo ou refinamento consegue contorná-lo: não
    # existe caminho que produza fala automática sem passar por aqui.
    @property
    def pode_avisar(self) -> bool:
        """Frase de espera enquanto o modelo pensa."""
        return self.fala_automatica and self.ack_habilitado

    @property
    def pode_progredir(self) -> bool:
        """"Ainda estou nisso", em tarefa longa."""
        return self.fala_automatica and self.progresso_falado

    @property
    def pode_anunciar_ferramenta(self) -> bool:
        """"Deixa eu ver isso" antes de câmera ou tool."""
        return self.fala_automatica and self.anunciar_ferramenta

    @property
    def pode_saudar(self) -> bool:
        """Saudação ao subir o loop de voz, sem pergunta nenhuma."""
        return self.fala_automatica and self.saudacao_habilitada

    @property
    def alguma_fala_automatica(self) -> bool:
        """Vale sintetizar alguma frase automática neste arranque?

        Sem isto o app gasta seis chamadas de TTS (≈11 s medidos no robô) para
        encher um cache de frases que nunca vai tocar.
        """
        return self.pode_avisar or self.pode_progredir

    def motivo_silencio(self) -> str:
        """Por que não vai falar. Vai no evento, para o painel não adivinhar."""
        return ("automatic_speech_disabled" if not self.fala_automatica
                else "spoken_acknowledgements_disabled")

    def prazo_ack(self, inicio: float) -> float:
        """Instante absoluto do aviso. Absoluto, e não relativo ao anterior."""
        return inicio + self.ack_atraso_ms / 1000.0

    def prazo_progresso(self, inicio: float) -> float:
        return inicio + self.progresso_atraso_ms / 1000.0


def decidir_corte(restante_ms: float | None, corte_ms: int,
                  teto_desconhecido_ms: int = 2000) -> str:
    """`aguardar` ou `cortar`, quando a resposta fica pronta com o aviso tocando.

    Cortar sempre soaria truncado no meio de uma palavra quando falta pouco;
    nunca cortar acrescentaria segundos justamente quando a resposta já estava
    pronta. O limite resolve os dois.

    Sem duração conhecida, corta depois de um teto curto em vez de travar a
    resposta esperando um áudio de tamanho desconhecido.
    """
    if restante_ms is None:
        return "cortar" if teto_desconhecido_ms <= 0 else "aguardar"
    return "aguardar" if restante_ms <= corte_ms else "cortar"


@dataclass
class Turno:
    """Um turno de conversa. `id` é o que impede fala de turno velho."""

    id: str
    correlacao: str
    inicio: float
    politica: Politica
    cancelado: bool = False
    substituido_por: str | None = None
    ack_estado: str = AGENDADO
    ack_inicio: float | None = None
    ack_duracao_ms: float | None = None
    ack_decisao: str = "none"
    corte_falhou: bool = False
    progressos: int = 0
    metricas: dict[str, Any] = field(default_factory=dict)

    def restante_ack_ms(self, agora: float | None = None) -> float | None:
        """Quanto falta do aviso, pela duração real do áudio sintetizado."""
        if self.ack_inicio is None or self.ack_duracao_ms is None:
            return None
        decorrido = ((agora or time.monotonic()) - self.ack_inicio) * 1000.0
        return max(0.0, self.ack_duracao_ms - decorrido)

    @property
    def vivo(self) -> bool:
        return not self.cancelado and self.substituido_por is None


class CoordenadorAudio:
    """Único dono do alto-falante. Ninguém mais chama push/clear.

    O lock cobre transição de estado e o comando ao player — nunca a duração da
    reprodução. Segurar o lock enquanto o áudio toca faria a resposta final
    esperar o aviso terminar para poder cortá-lo, que é exatamente o deadlock
    que este desenho evita.
    """

    def __init__(self, media: Any, sr_saida: int, log: Any,
                 relogio: Callable[[], float] = time.monotonic) -> None:
        self._media = media
        self._sr = sr_saida
        self._log = log
        self._agora = relogio
        self._lock = threading.Lock()
        self._turno_atual: str | None = None
        self._tipo: str | None = None   # "acknowledgement" | "final_response"

    def abrir(self, turno: Turno) -> None:
        with self._lock:
            self._turno_atual = turno.id
            self._tipo = None

    def tocar_ack(self, turno: Turno, onda) -> bool:
        """Enfileira o aviso. `False` se o turno já não vale."""
        with self._lock:
            if not turno.vivo or self._turno_atual != turno.id:
                turno.ack_estado = CANCELADO
                return False
            self._empurrar(onda)
            self._tipo = "acknowledgement"
            turno.ack_estado = TOCANDO
            turno.ack_inicio = self._agora()
            turno.ack_duracao_ms = len(onda) / self._sr * 1000.0
        return True

    def resolver_ack(self, turno: Turno) -> str:
        """Decide o que fazer com o aviso agora que a resposta chegou.

        Devolve `none` (nunca tocou), `cancelled` (agendado, não chegou a
        tocar), `completed` (deixou terminar) ou `flushed` (cortado).
        """
        with self._lock:
            if turno.ack_estado == DESABILITADO:
                # Nada foi sintetizado, nada entrou na fila, nada a cortar.
                turno.ack_decisao = DESABILITADO
                return DESABILITADO
            if turno.ack_estado == AGENDADO:
                turno.ack_estado = turno.ack_decisao = CANCELADO
                return CANCELADO
            if turno.ack_estado != TOCANDO:
                turno.ack_decisao = "none"
                return "none"
            restante = turno.restante_ack_ms(self._agora())
            decisao = decidir_corte(restante, turno.politica.corte_ms)
            turno.metricas["ack_restante_ms"] = round(restante or -1.0, 1)
            if decisao == "cortar":
                if self._cortar(turno):
                    turno.ack_estado = turno.ack_decisao = CORTADO
                    return CORTADO
                # Falhou o flush: esperar é a única saída sem sobrepor voz.
                turno.corte_falhou = True
        # Fora do lock: esperar o áudio terminar não pode bloquear quem quer
        # cancelar o turno (barge-in, e-stop).
        #
        # Duração desconhecida (TTS não informou, áudio vazio) cai no teto
        # conservador em vez de seguir direto — seguir direto sobreporia voz,
        # que é o defeito que este módulo existe para eliminar.
        restante = turno.restante_ack_ms()
        if restante is None:
            restante = TETO_DESCONHECIDO_MS
        if restante:
            time.sleep(min(restante / 1000.0, 3.0))
        with self._lock:
            turno.ack_estado = turno.ack_decisao = CONCLUIDO
        return CONCLUIDO

    def tocar_final(self, turno: Turno, ondas) -> bool:
        """Enfileira a resposta. `False` se outro turno já assumiu."""
        with self._lock:
            if not turno.vivo or self._turno_atual != turno.id:
                return False
            self._tipo = "final_response"
            for onda in ondas:
                self._empurrar(onda)
        return True

    def cancelar(self, turno: Turno, motivo: str = "cancelled") -> None:
        """Barge-in, e-stop ou pergunta nova: corta agora, sem limite de tempo.

        O limite de 1,2 s vale só para a transição automática aviso → resposta.
        Quando é o usuário que interrompe, esperar seria o oposto do pedido.
        """
        with self._lock:
            turno.cancelado = True
            if turno.ack_estado in (AGENDADO, TOCANDO):
                turno.ack_estado = CANCELADO
            if self._turno_atual == turno.id:
                self._cortar(turno)
                self._turno_atual = None
                self._tipo = None
        self._log.info("Turno %s cancelado (%s).", turno.id, motivo)

    # ── internos, sempre sob o lock ────────────────────────────────────────
    def _empurrar(self, onda) -> None:
        self._media.push_audio_sample(onda)

    def _cortar(self, turno: Turno) -> bool:
        """Flush do player. Só sobre o áudio DESTE turno, nunca do seguinte."""
        if self._turno_atual != turno.id:
            return False
        audio = getattr(self._media, "audio", None)
        limpar = getattr(audio, "clear_player", None)
        if limpar is None:
            self._log.debug("SDK sem clear_player; não dá para cortar o áudio.")
            return False
        try:
            limpar()
            return True
        except Exception:
            self._log.warning("clear_player falhou; vou esperar o áudio terminar.",
                              exc_info=True)
            return False


def perfil_atualizado(conf: dict, mudancas: dict) -> dict:
    """Aplica mudanças preservando o que o usuário ajustou em cada perfil.

    Trocar de modo **não** reescreve os tempos: cada perfil guarda o seu, então
    voltar ao modo anterior recupera o ajuste que estava lá.
    """
    c = normalizar(conf)
    if "mode" in mudancas:
        modo = str(mudancas["mode"]).strip().lower()
        if modo not in MODOS:
            raise ValueError(f"modo desconhecido: {mudancas['mode']!r}")
        c["mode"] = modo
    for chave in BOOLEANOS:
        if chave in mudancas:
            c[chave] = bool(mudancas[chave])
    for campo in ("progress_update_delay_ms", "max_progress_messages",
                  "acknowledgement_cut_threshold_ms"):
        if campo in mudancas:
            c[campo] = _inteiro(mudancas[campo], c[campo], *LIMITES[campo])
    # O atraso do aviso pertence ao perfil que está sendo editado.
    if "acknowledgement_delay_ms" in mudancas:
        c["profiles"][c["mode"]]["acknowledgement_delay_ms"] = _inteiro(
            mudancas["acknowledgement_delay_ms"],
            c["profiles"][c["mode"]]["acknowledgement_delay_ms"],
            *LIMITES["acknowledgement_delay_ms"])
    return c


__all__ = [
    "DESABILITADO", "PADRAO_ARRANQUE", "normalizar_arranque",
    "AGENDADO", "CANCELADO", "CONCLUIDO", "CORTADO", "TOCANDO",
    "CoordenadorAudio", "MODOS", "PADRAO", "Politica", "Turno",
    "TETO_DESCONHECIDO_MS", "decidir_corte", "normalizar",
    "perfil_atualizado",
]
