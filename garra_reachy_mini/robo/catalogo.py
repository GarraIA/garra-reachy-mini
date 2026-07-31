"""Catálogo de ações: a allowlist do que o robô pode fazer.

Nada executa fora daqui. O modelo não manda pose, não manda ângulo cru e não
manda nome de move arbitrário — manda o nome de uma ação deste catálogo com
parâmetros que passam por JSON Schema e depois por `limites.py`.

Cada ação declara:

  `schema`               JSON Schema (vai para o MCP e valida a entrada da API)
  `movimento`            se toca fisicamente no robô (define o que o e-stop bloqueia)
  `prioridade_padrao`    0 emergência · 1 explícita · 2 ambiente
  `politica`             o que fazer quando outra explícita já está rodando
  `permitido_em_estop`   se continua valendo com o robô travado

Nomes de expressão são **aliases canônicos**, não nomes de arquivo: `happy` é o
nosso contrato, `cheerful1` é o que existe hoje na biblioteca do HuggingFace. A
resolução acontece no arranque contra a lista real do daemon
(`resolver_expressoes`), e o que não existir aparece como indisponível em
`/api/robot/capabilities` em vez de estourar na hora de executar.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from . import limites
from .daemon_api import DATASET_DANCAS, DATASET_EMOCOES

# ─── erros que o executor sabe classificar ───────────────────────────────────


class AcaoCancelada(Exception):
    """A ação foi interrompida (preempção ou e-stop). Não é falha."""


class AcaoFalhou(Exception):
    """A ação não completou. Vira `executed: false` com motivo."""


class AcaoExpirou(AcaoFalhou):
    """Estourou o timeout."""


# ─── contexto entregue ao handler ────────────────────────────────────────────


@dataclass
class Contexto:
    """O que um handler pode usar. Montado pelo `ControladorRobo`."""

    backend: Any
    lim: limites.Limitador
    timeout: float
    cancelado: Callable[[], bool]
    expressoes: dict[str, str | None]
    catalogo_moves: Callable[[str], list[str]]
    tracking_pedir: Callable[[bool, float, str], dict[str, Any]]
    aleatorio: random.Random
    # O controlador guarda aqui o id do move em voo. É o que permite à parada de
    # emergência mandar UM `POST /api/move/stop` direto, sem antes perguntar ao
    # daemon o que está rodando — a diferença entre ~240 ms e ~60 ms de latência.
    registrar_ident: Callable[[str | None], None] = lambda _ident: None

    def checar_cancelamento(self) -> None:
        if self.cancelado():
            raise AcaoCancelada()

    def mover(self, **kwargs: Any) -> None:
        """Um `goto` no daemon + espera cancelável. O primitivo de movimento."""
        self.checar_cancelamento()
        ident = self.backend.goto(**kwargs)
        self._aguardar(ident)

    def tocar(self, dataset: str, nome: str) -> None:
        self.checar_cancelamento()
        ident = self.backend.tocar_move(dataset, nome)
        self._aguardar(ident)

    def _aguardar(self, ident: str) -> None:
        from .backends import CANCELADO, EXPIRADO

        self.registrar_ident(ident)
        try:
            resultado = self.backend.esperar(ident, self.timeout, self.cancelado)
        finally:
            self.registrar_ident(None)
        if resultado == CANCELADO:
            raise AcaoCancelada()
        if resultado == EXPIRADO:
            raise AcaoExpirou(f"o movimento passou de {self.timeout:g}s sem terminar")


# ─── expressões canônicas ────────────────────────────────────────────────────
# Ordem = preferência. A primeira que existir na biblioteca do daemon vence.
EXPRESSOES: dict[str, tuple[str, ...]] = {
    "neutral": (),  # caso especial: volta à pose inicial, sem move gravado
    "happy": ("cheerful1", "enthusiastic1", "laughing1"),
    "sad": ("sad1", "sad2", "downcast1"),
    "curious": ("curious1", "inquiring1", "inquiring2"),
    "surprised": ("surprised1", "surprised2", "amazed1"),
    "confused": ("confused1", "uncertain1", "incomprehensible2"),
    "excited": ("enthusiastic1", "enthusiastic2", "cheerful1"),
    "sleepy": ("tired1", "sleep1", "exhausted1"),
    "attentive": ("attentive1", "attentive2", "understanding1"),
    "greeting": ("welcoming1", "welcoming2"),
    "yes": ("yes1",),
    "no": ("no1",),
    "proud": ("proud1", "proud2", "proud3"),
    "grateful": ("grateful1",),
    "bored": ("boredom1", "boredom2"),
    "scared": ("scared1", "fear1", "anxiety1"),
    "angry": ("irritated1", "furious1", "rage1"),
    "relieved": ("relief1", "relief2", "serenity1"),
    "thoughtful": ("thoughtful1", "thoughtful2"),
    "impatient": ("impatient1", "impatient2"),
    "loving": ("loving1",),
}

# As nove que a interface promete numa fileira só.
EXPRESSOES_PRINCIPAIS = (
    "neutral", "happy", "sad", "curious", "surprised",
    "confused", "excited", "sleepy", "attentive",
)


def resolver_expressoes(disponiveis: list[str]) -> dict[str, str | None]:
    """Mapeia alias canônico → nome real, ou None se nenhum existir.

    Chamado no arranque com a lista que o daemon devolve. Assim uma mudança na
    biblioteca vira um aviso na interface, não um erro em tempo de execução.
    """
    conjunto = set(disponiveis)
    saida: dict[str, str | None] = {}
    for alias, candidatos in EXPRESSOES.items():
        if not candidatos:  # neutral
            saida[alias] = None
            continue
        saida[alias] = next((c for c in candidatos if c in conjunto), None)
    return saida


# ─── handlers ────────────────────────────────────────────────────────────────


def _pose_neutra(ctx: Contexto) -> np.ndarray:
    return limites.pose_cabeca(ctx.lim)


def _h_turn_head(ctx: Contexto, p: dict[str, Any]) -> str:
    direcao = p.get("direction", "center")
    intensidade = ctx.lim.intensidade(p.get("intensity"))
    duracao = ctx.lim.duracao(p.get("duration"))
    pose = limites.pose_direcao(ctx.lim, direcao, intensidade)
    ctx.mover(pose=pose, duracao=duracao)
    nomes = {
        "left": "para a esquerda", "right": "para a direita",
        "up": "para cima", "down": "para baixo", "center": "para o centro",
        "up_left": "para cima e à esquerda", "up_right": "para cima e à direita",
        "down_left": "para baixo e à esquerda", "down_right": "para baixo e à direita",
    }
    return f"O robô virou a cabeça {nomes.get(direcao, direcao)}."


def _h_look_at(ctx: Contexto, p: dict[str, Any]) -> str:
    duracao = ctx.lim.duracao(p.get("duration"))
    alvo = p.get("target")

    if alvo == "user":
        # "Olhe para mim" = rastreamento de rosto nativo (YuNet no daemon), não
        # uma pose fixa: o usuário se mexe.
        estado = ctx.tracking_pedir(True, ctx.lim.peso_tracking(p.get("weight")), "usuario")
        if not estado.get("enabled"):
            # Sem câmera: cai para "olhar para a frente", e diz que caiu.
            ctx.mover(pose=_pose_neutra(ctx), duracao=duracao)
            return (
                "Não consegui ligar o rastreamento de rosto (câmera indisponível); "
                "o robô ficou olhando para a frente."
            )
        return "O robô está rastreando o rosto e olhando para o usuário."

    # As duas variantes abaixo recebem a pose PRONTA do SDK, calculada a partir
    # de um ponto no espaço — e nada garante que esse ponto caiba no envelope.
    # `limitar_pose` aplica o mesmo limite de `pose_cabeca` e ainda registra o
    # que ajustou, para a resposta poder dizer.
    if "u" in p and "v" in p:
        pose = ctx.backend.pose_olhar_imagem(p["u"], p["v"])
        if pose is None:
            raise AcaoFalhou("olhar para um pixel exige câmera calibrada, que não está disponível")
        ctx.mover(pose=limites.limitar_pose(ctx.lim, pose), duracao=duracao)
        return f"O robô olhou para o ponto ({int(p['u'])}, {int(p['v'])}) da imagem."

    if any(k in p for k in ("x", "y", "z")):
        x = float(p.get("x", limites.DISTANCIA_OLHAR_M))
        y = float(p.get("y", 0.0))
        z = float(p.get("z", 0.0))
        pose = limites.limitar_pose(ctx.lim, ctx.backend.pose_olhar_mundo(x, y, z))
        ctx.mover(pose=pose, duracao=duracao)
        return f"O robô olhou para o ponto ({x:g}, {y:g}, {z:g}) do espaço."

    if alvo in limites.DIRECOES:
        return _h_turn_head(ctx, {"direction": alvo, "intensity": p.get("intensity"), "duration": duracao})

    raise AcaoFalhou("look_at precisa de `target`, de (x, y, z) ou de (u, v)")


def _h_set_expression(ctx: Contexto, p: dict[str, Any]) -> str:
    nome = str(p.get("name", "neutral")).lower()
    if nome not in EXPRESSOES:
        raise AcaoFalhou(f"expressão desconhecida: {nome!r}")
    if nome == "neutral":
        ctx.mover(
            pose=_pose_neutra(ctx),
            antenas=[0.0, 0.0],
            duracao=ctx.lim.duracao(p.get("duration") or 0.8),
        )
        return "O robô voltou à expressão neutra."
    real = ctx.expressoes.get(nome)
    if not real:
        raise AcaoFalhou(
            f"a expressão {nome!r} não existe na biblioteca instalada no robô"
        )
    ctx.tocar(DATASET_EMOCOES, real)
    return f"O robô fez a expressão {nome} ({real})."


def _h_move_antennas(ctx: Contexto, p: dict[str, Any]) -> str:
    duracao = ctx.lim.duracao(p.get("duration"))
    preset = p.get("preset")
    if preset == "wiggle":
        for lado in (1, -1, 1, -1):
            ctx.mover(antenas=ctx.lim.antenas(0.4 * lado, -0.4 * lado), duracao=0.22)
        ctx.mover(antenas=[0.0, 0.0], duracao=0.3)
        return "O robô mexeu as antenas."
    presets = {"up": (0.8, 0.8), "down": (-0.8, -0.8), "neutral": (0.0, 0.0)}
    if preset in presets:
        esq, dir_ = presets[preset]
    else:
        esq = float(p.get("left", 0.0))
        dir_ = float(p.get("right", 0.0))
    ctx.mover(antenas=ctx.lim.antenas(esq, dir_), duracao=duracao)
    return f"O robô posicionou as antenas (esquerda {esq:g} rad, direita {dir_:g} rad)."


def _h_nod(ctx: Contexto, p: dict[str, Any]) -> str:
    vezes = int(min(max(int(p.get("times", 2)), 1), 5))
    i = ctx.lim.intensidade(p.get("intensity") or 0.55)
    for _ in range(vezes):
        ctx.mover(pose=limites.pose_cabeca(ctx.lim, pitch_deg=limites.MAX_PITCH_DEG * i), duracao=0.28)
        ctx.mover(pose=limites.pose_cabeca(ctx.lim, pitch_deg=-limites.MAX_PITCH_DEG * i * 0.35), duracao=0.28)
    ctx.mover(pose=_pose_neutra(ctx), duracao=0.3)
    return f"O robô fez que sim com a cabeça ({vezes}x)."


def _h_shake_head(ctx: Contexto, p: dict[str, Any]) -> str:
    vezes = int(min(max(int(p.get("times", 2)), 1), 5))
    i = ctx.lim.intensidade(p.get("intensity") or 0.5)
    for _ in range(vezes):
        ctx.mover(pose=limites.pose_cabeca(ctx.lim, yaw_deg=limites.MAX_YAW_DEG * i), duracao=0.26)
        ctx.mover(pose=limites.pose_cabeca(ctx.lim, yaw_deg=-limites.MAX_YAW_DEG * i), duracao=0.26)
    ctx.mover(pose=_pose_neutra(ctx), duracao=0.3)
    return f"O robô fez que não com a cabeça ({vezes}x)."


def _h_run_movement(ctx: Contexto, p: dict[str, Any]) -> str:
    biblioteca = str(p.get("library", "emotions")).lower()
    dataset = DATASET_DANCAS if biblioteca in ("dances", "dancas", "dança", "dancas") else DATASET_EMOCOES
    nome = str(p.get("name", "")).strip()
    disponiveis = ctx.catalogo_moves(dataset)
    if nome not in disponiveis:
        raise AcaoFalhou(
            f"movimento {nome!r} não existe em {biblioteca}. "
            f"Disponíveis: {', '.join(sorted(disponiveis)[:12])}…"
        )
    ctx.tocar(dataset, nome)
    return f"O robô executou o movimento {nome} ({biblioteca})."


def _h_dance(ctx: Contexto, p: dict[str, Any]) -> str:
    disponiveis = ctx.catalogo_moves(DATASET_DANCAS)
    if not disponiveis:
        raise AcaoFalhou("a biblioteca de danças não está disponível no robô")
    nome = str(p.get("name") or "").strip() or ctx.aleatorio.choice(disponiveis)
    if nome not in disponiveis:
        raise AcaoFalhou(f"dança {nome!r} não existe. Disponíveis: {', '.join(sorted(disponiveis))}")
    ctx.tocar(DATASET_DANCAS, nome)
    return f"O robô dançou ({nome})."


def _h_greet(ctx: Contexto, p: dict[str, Any]) -> str:
    real = ctx.expressoes.get("greeting")
    if real:
        ctx.tocar(DATASET_EMOCOES, real)
        return f"O robô cumprimentou ({real})."
    # Sem a biblioteca: um aceno feito à mão, para `greet` nunca só falhar.
    for lado in (1, -1, 1):
        ctx.mover(
            pose=limites.pose_cabeca(ctx.lim, yaw_deg=18.0 * lado, roll_deg=8.0 * lado),
            antenas=ctx.lim.antenas(0.6, 0.6),
            duracao=0.3,
        )
    ctx.mover(pose=_pose_neutra(ctx), antenas=[0.0, 0.0], duracao=0.4)
    return "O robô cumprimentou com um aceno de cabeça."


def _h_face_tracking(ctx: Contexto, p: dict[str, Any]) -> str:
    ligado = bool(p.get("enabled", True))
    peso = ctx.lim.peso_tracking(p.get("weight"))
    estado = ctx.tracking_pedir(ligado, peso, "usuario")
    if ligado and not estado.get("enabled"):
        raise AcaoFalhou(
            "o daemon recusou o rastreamento (provavelmente sem servidor de mídia/câmera)"
        )
    return (
        f"Rastreamento de rosto ligado (peso {peso:g})."
        if ligado
        else "Rastreamento de rosto desligado."
    )


def _h_wake_up(ctx: Contexto, p: dict[str, Any]) -> str:
    ctx.backend.preparar()
    ctx.checar_cancelamento()
    ident = ctx.backend.wake_up()
    ctx._aguardar(ident)
    return "O robô acordou."


def _h_sleep(ctx: Contexto, p: dict[str, Any]) -> str:
    ctx.checar_cancelamento()
    ident = ctx.backend.goto_sleep()
    ctx._aguardar(ident)
    return "O robô foi dormir."


def _h_return_to_neutral(ctx: Contexto, p: dict[str, Any]) -> str:
    ctx.backend.preparar()
    ctx.mover(
        pose=_pose_neutra(ctx),
        antenas=[0.0, 0.0],
        body_yaw=0.0,
        duracao=ctx.lim.duracao(p.get("duration") or 1.0),
    )
    return "O robô voltou à posição neutra."


# ─── definição das ações ─────────────────────────────────────────────────────

_DIRECOES_ENUM = sorted(limites.DIRECOES)
_DURACAO = {
    "type": "number",
    "minimum": limites.DURACAO_MIN_S,
    "maximum": limites.DURACAO_MAX_S,
    "description": f"Duração em segundos ({limites.DURACAO_MIN_S}–{limites.DURACAO_MAX_S}).",
}
_INTENSIDADE = {
    "type": "number",
    "minimum": 0.0,
    "maximum": 1.0,
    "description": "Amplitude relativa, 0 a 1. 1 = o máximo seguro configurado.",
}


@dataclass(frozen=True)
class Acao:
    nome: str
    descricao: str
    schema: dict[str, Any]
    handler: Callable[[Contexto, dict[str, Any]], str] | None = None
    movimento: bool = True
    prioridade_padrao: int = 1
    politica: str = "preempt"  # preempt | queue | reject
    permitido_em_estop: bool = False
    timeout_s: float = limites.TIMEOUT_PADRAO_S
    # Ações tratadas pelo próprio controlador (stop, clear_estop, status…).
    interna: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)


def _obj(props: dict[str, Any], obrigatorios: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": list(obrigatorios),
        "additionalProperties": False,
    }


ACOES: tuple[Acao, ...] = (
    # ── internas (não tocam no robô por um handler) ──────────────────────────
    Acao(
        "status",
        "Estado atual do robô: conexão, motores, movimento em curso, modo real ou simulado.",
        _obj({}),
        movimento=False, interna=True, permitido_em_estop=True, tags=("leitura",),
    ),
    Acao(
        "stop",
        "PARADA DE EMERGÊNCIA: interrompe qualquer movimento, dança ou expressão agora "
        "e trava o robô. Não volta à posição neutra sozinho.",
        _obj({}),
        movimento=False, interna=True, prioridade_padrao=0,
        permitido_em_estop=True, tags=("seguranca",),
    ),
    Acao(
        "clear_estop",
        "Libera o robô depois de uma parada de emergência. Só isso — não move nada.",
        _obj({}),
        movimento=False, interna=True, permitido_em_estop=True, tags=("seguranca",),
    ),
    Acao(
        "disable_motors",
        "Desliga o torque dos motores. A cabeça fica solta. Use só se o robô estiver preso ou forçando.",
        _obj({}),
        movimento=False, interna=True, permitido_em_estop=True, tags=("seguranca",),
    ),
    Acao(
        "enable_motors",
        "Religa o torque dos motores.",
        _obj({}),
        movimento=False, interna=True, tags=("seguranca",),
    ),
    Acao(
        "capture_image",
        "Captura um quadro da câmera do robô e salva em disco. Devolve caminho e dimensões, "
        "não a imagem: para SABER o que aparece, use a ferramenta de visão (olhos__olhar).",
        _obj({}),
        movimento=False, interna=True, permitido_em_estop=True, tags=("camera",),
    ),
    Acao(
        "list_apps",
        "Lista os aplicativos instalados no robô e qual está rodando.",
        _obj({}),
        movimento=False, interna=True, permitido_em_estop=True, tags=("apps",),
    ),
    Acao(
        "start_app",
        "Inicia um aplicativo do robô pelo nome. Só um app roda por vez; o atual é parado.",
        _obj({"name": {"type": "string", "description": "Nome exato do app instalado."}}, ("name",)),
        movimento=False, interna=True, tags=("apps",),
    ),
    Acao(
        "stop_app",
        "Para o aplicativo que estiver rodando no robô.",
        _obj({}),
        movimento=False, interna=True, tags=("apps",),
    ),
    Acao(
        "restart_app",
        "Reinicia o aplicativo que estiver rodando no robô.",
        _obj({}),
        movimento=False, interna=True, tags=("apps",),
    ),
    Acao(
        "face_tracking",
        "Liga ou desliga o rastreamento de rosto nativo do robô (a cabeça acompanha quem estiver na frente).",
        _obj({
            "enabled": {"type": "boolean", "description": "true liga, false desliga."},
            "weight": {"type": "number", "minimum": 0.0, "maximum": 1.0,
                       "description": "Quanto o rosto manda na cabeça. 1 = totalmente."},
        }, ("enabled",)),
        handler=_h_face_tracking, movimento=False, tags=("visao",),
    ),
    # ── movimento ────────────────────────────────────────────────────────────
    Acao(
        "turn_head",
        "Vira a cabeça do robô numa direção.",
        _obj({
            "direction": {"type": "string", "enum": _DIRECOES_ENUM,
                          "description": "Direção do ponto de vista de quem olha o robô."},
            "intensity": _INTENSIDADE,
            "duration": _DURACAO,
        }, ("direction",)),
        handler=_h_turn_head, tags=("cabeca",),
    ),
    Acao(
        "look_at",
        "Faz o robô olhar para algo: para o usuário (liga o rastreamento de rosto), "
        "para uma direção, para um ponto do espaço (x, y, z em metros) ou para um pixel (u, v).",
        _obj({
            "target": {"type": "string", "enum": ["user", *_DIRECOES_ENUM],
                       "description": "'user' rastreia o rosto; o resto é direção fixa."},
            "x": {"type": "number", "description": "Metros para a frente."},
            "y": {"type": "number", "description": "Metros para a esquerda."},
            "z": {"type": "number", "description": "Metros para cima."},
            "u": {"type": "number", "description": "Coluna do pixel na imagem da câmera."},
            "v": {"type": "number", "description": "Linha do pixel na imagem da câmera."},
            "weight": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "intensity": _INTENSIDADE,
            "duration": _DURACAO,
        }),
        handler=_h_look_at, tags=("cabeca", "visao"),
    ),
    Acao(
        "set_expression",
        "Faz o robô demonstrar uma emoção com movimento de cabeça, antenas e som.",
        _obj({
            "name": {"type": "string", "enum": sorted(EXPRESSOES),
                     "description": "Nome canônico da expressão."},
            "duration": _DURACAO,
        }, ("name",)),
        handler=_h_set_expression, timeout_s=25.0, tags=("expressao",),
    ),
    Acao(
        "move_antennas",
        "Move as antenas do robô.",
        _obj({
            "preset": {"type": "string", "enum": ["up", "down", "neutral", "wiggle"]},
            "left": {"type": "number", "minimum": -limites.MAX_ANTENA_RAD,
                     "maximum": limites.MAX_ANTENA_RAD, "description": "Ângulo em radianos."},
            "right": {"type": "number", "minimum": -limites.MAX_ANTENA_RAD,
                      "maximum": limites.MAX_ANTENA_RAD, "description": "Ângulo em radianos."},
            "duration": _DURACAO,
        }),
        handler=_h_move_antennas, tags=("antenas",),
    ),
    Acao(
        "nod",
        "Faz que sim com a cabeça.",
        _obj({"times": {"type": "integer", "minimum": 1, "maximum": 5}, "intensity": _INTENSIDADE}),
        handler=_h_nod, tags=("cabeca",),
    ),
    Acao(
        "shake_head",
        "Faz que não com a cabeça.",
        _obj({"times": {"type": "integer", "minimum": 1, "maximum": 5}, "intensity": _INTENSIDADE}),
        handler=_h_shake_head, tags=("cabeca",),
    ),
    Acao(
        "greet",
        "Cumprimenta: aceno de cabeça e antenas.",
        _obj({}),
        handler=_h_greet, timeout_s=25.0, tags=("expressao",),
    ),
    Acao(
        "dance",
        "Faz o robô dançar. Sem `name`, escolhe uma dança da biblioteca ao acaso.",
        _obj({"name": {"type": "string", "description": "Nome da dança; em branco = aleatória."}}),
        handler=_h_dance, timeout_s=60.0, tags=("danca",),
    ),
    Acao(
        "run_movement",
        "Executa um movimento gravado pelo nome exato, da biblioteca de emoções ou de danças.",
        _obj({
            "library": {"type": "string", "enum": ["emotions", "dances"]},
            "name": {"type": "string"},
        }, ("library", "name")),
        handler=_h_run_movement, timeout_s=60.0, tags=("movimento",),
    ),
    Acao(
        "return_to_neutral",
        "Leva o robô de volta à posição inicial: cabeça centrada e antenas em repouso.",
        _obj({"duration": _DURACAO}),
        handler=_h_return_to_neutral, tags=("cabeca",),
    ),
    Acao(
        "wake_up",
        "Acorda o robô: liga os motores, vai para a posição inicial e toca o som de despertar.",
        _obj({}),
        handler=_h_wake_up, timeout_s=25.0, tags=("energia",),
    ),
    Acao(
        "sleep",
        "Põe o robô para dormir: pose de repouso e som de desligar.",
        _obj({}),
        handler=_h_sleep, timeout_s=25.0, tags=("energia",),
    ),
)

CATALOGO: dict[str, Acao] = {a.nome: a for a in ACOES}

# O que continua valendo com o robô em parada de emergência. Além das três que a
# revisão exigiu, deixamos passar as leituras puras (`status`, `list_apps`,
# `capture_image`): elas não conseguem mover o robô por construção, e é
# justamente durante um e-stop que se quer olhar o que aconteceu.
PERMITIDAS_EM_ESTOP = frozenset(a.nome for a in ACOES if a.permitido_em_estop)


def descrever_catalogo() -> list[dict[str, Any]]:
    """Catálogo em JSON, para `/api/robot/capabilities` e para o MCP."""
    return [
        {
            "name": a.nome,
            "description": a.descricao,
            "schema": a.schema,
            "movement": a.movimento,
            "priority": a.prioridade_padrao,
            "policy": a.politica,
            "allowed_in_estop": a.permitido_em_estop,
            "timeout_s": a.timeout_s,
            "tags": list(a.tags),
        }
        for a in ACOES
    ]


def validar(nome: str, params: dict[str, Any]) -> dict[str, Any]:
    """Valida contra o schema da ação. Devolve os parâmetros limpos.

    Validação artesanal de propósito: `jsonschema` não é dependência do projeto e
    os schemas aqui usam só um punhado de construções. O que importa é rejeitar
    campo desconhecido, tipo errado e valor fora do enum — o clamp numérico fica
    com `limites.py`, que também sabe *relatar* o que ajustou.
    """
    acao = CATALOGO.get(nome)
    if acao is None:
        raise AcaoFalhou(f"ação desconhecida: {nome!r}")
    schema = acao.schema
    props: dict[str, Any] = schema.get("properties", {})
    limpos: dict[str, Any] = {}

    desconhecidos = sorted(set(params) - set(props))
    if desconhecidos:
        raise AcaoFalhou(f"parâmetros não aceitos por {nome}: {', '.join(desconhecidos)}")

    for faltando in schema.get("required", []):
        if params.get(faltando) is None:
            raise AcaoFalhou(f"{nome} exige o parâmetro {faltando!r}")

    for chave, valor in params.items():
        if valor is None:
            continue
        regra = props[chave]
        tipo = regra.get("type")
        if tipo == "boolean":
            if not isinstance(valor, bool):
                raise AcaoFalhou(f"{nome}.{chave} deve ser booleano")
        elif tipo == "integer":
            if isinstance(valor, bool) or not isinstance(valor, int):
                raise AcaoFalhou(f"{nome}.{chave} deve ser inteiro")
        elif tipo == "number":
            if isinstance(valor, bool) or not isinstance(valor, (int, float)):
                raise AcaoFalhou(f"{nome}.{chave} deve ser numérico")
            valor = float(valor)
        elif tipo == "string":
            if not isinstance(valor, str):
                raise AcaoFalhou(f"{nome}.{chave} deve ser texto")
            if "enum" in regra and valor not in regra["enum"]:
                raise AcaoFalhou(
                    f"{nome}.{chave}={valor!r} não é válido. Use um de: {', '.join(regra['enum'])}"
                )
        limpos[chave] = valor
    return limpos
