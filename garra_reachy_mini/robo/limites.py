"""Limites físicos e clamps. Nenhum ângulo chega ao robô sem passar por aqui.

Por que clampar do nosso lado, se o robô já tem limite mecânico e IK segura?
Porque as duas proteções do SDK têm buraco:

  • a IK com limite (`max_relative_yaw=65°`, `max_body_yaw=160°`) só é usada
    quando `automatic_body_yaw=True`. Continuamos com o padrão ligado, mas não
    queremos depender disso para não machucar o robô;
  • `set_target` **não tem clamp nenhum** de velocidade ou aceleração — escreve
    direto na IK na frequência em que for chamado. Por isso o primitivo desta
    camada é `goto_target` (min-jerk, duração explícita), nunca `set_target`.

Os valores abaixo são conservadores de propósito: bem dentro do envelope
mecânico medido (`assets/config/hardware_config.yaml` documenta os stewart em
−48°/+80°). Preferimos um robô com amplitude menor a um robô batendo no fim de
curso. `intensity` 1.0 = o limite abaixo, não o limite do hardware.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt

# ─── Envelope da cabeça (intensity = 1.0) ────────────────────────────────────
MAX_YAW_DEG = 45.0
MAX_PITCH_DEG = 28.0
MAX_ROLL_DEG = 22.0
MAX_X_MM = 15.0
MAX_Y_MM = 15.0
MAX_Z_MM = 20.0

# ─── Antenas ─────────────────────────────────────────────────────────────────
# Mecanicamente livres (0..4095 ticks), então o limite aqui é puramente estético
# e de segurança: ±1,5 rad ≈ ±86°.
MAX_ANTENA_RAD = 1.5

# ─── Tempo ───────────────────────────────────────────────────────────────────
# Mínimo de 0,15 s: abaixo disso o min-jerk vira um tranco.
DURACAO_MIN_S = 0.15
DURACAO_MAX_S = 6.0
DURACAO_PADRAO_S = 0.8

# Teto de espera de UMA ação. Move gravado mais longo das bibliotecas fica bem
# abaixo disso; o teto existe para a fila nunca travar por um move que não
# termina.
TIMEOUT_PADRAO_S = 15.0
TIMEOUT_MAX_S = 90.0

# ─── Tracking ────────────────────────────────────────────────────────────────
PESO_TRACKING_MIN = 0.0
PESO_TRACKING_MAX = 1.0
# Peso do tracking quando é o comportamento ambiente que o liga: acompanha o
# rosto sem sequestrar a cabeça de quem estiver comandando.
PESO_TRACKING_AMBIENTE = 0.35

# Distância de "olhar para" no mundo, em metros (x para a frente).
DISTANCIA_OLHAR_M = 1.0


@dataclass
class Ajuste:
    """Um valor que foi limitado. Vira mensagem honesta para o usuário/modelo."""

    campo: str
    pedido: float
    aplicado: float

    def __str__(self) -> str:
        return f"{self.campo}: pedido {self.pedido:g}, aplicado {self.aplicado:g}"


@dataclass
class Limitador:
    """Acumula os ajustes feitos para que a resposta possa relatá-los.

    Sem isso a ação responderia "virei a cabeça 90°" tendo virado 45 — que é
    exatamente o tipo de mentira que esta camada existe para evitar.
    """

    ajustes: list[Ajuste] = field(default_factory=list)

    def valor(self, campo: str, v: float, minimo: float, maximo: float) -> float:
        limitado = min(max(float(v), minimo), maximo)
        if not _quase_igual(limitado, float(v)):
            self.ajustes.append(Ajuste(campo, float(v), limitado))
        return limitado

    def duracao(self, v: float | None) -> float:
        if v is None:
            return DURACAO_PADRAO_S
        return self.valor("duration", v, DURACAO_MIN_S, DURACAO_MAX_S)

    def timeout(self, v: float | None, minimo: float = 1.0) -> float:
        if v is None:
            return TIMEOUT_PADRAO_S
        return self.valor("timeout", v, minimo, TIMEOUT_MAX_S)

    def intensidade(self, v: float | None) -> float:
        if v is None:
            return 1.0
        return self.valor("intensity", v, 0.0, 1.0)

    def peso_tracking(self, v: float | None) -> float:
        if v is None:
            return PESO_TRACKING_MAX
        return self.valor("weight", v, PESO_TRACKING_MIN, PESO_TRACKING_MAX)

    def antenas(self, esquerda: float | None, direita: float | None) -> list[float]:
        """Devolve [direita, esquerda] — a ordem que o SDK espera.

        O SDK usa `[right_angle, left_angle]` (comentário em `reachy_mini.py:588`).
        A nossa API pública fala "esquerda"/"direita" porque é o que o usuário e o
        modelo dizem; a inversão acontece só aqui, num lugar só.
        """
        d = self.valor("antenna_right", direita or 0.0, -MAX_ANTENA_RAD, MAX_ANTENA_RAD)
        e = self.valor("antenna_left", esquerda or 0.0, -MAX_ANTENA_RAD, MAX_ANTENA_RAD)
        return [d, e]

    def resumo(self) -> str:
        return "; ".join(str(a) for a in self.ajustes)


def _quase_igual(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol


def pose_cabeca(
    lim: Limitador,
    *,
    yaw_deg: float = 0.0,
    pitch_deg: float = 0.0,
    roll_deg: float = 0.0,
    x_mm: float = 0.0,
    y_mm: float = 0.0,
    z_mm: float = 0.0,
) -> npt.NDArray[np.float64]:
    """Matriz 4x4 homogênea da cabeça, já dentro do envelope.

    Convenção do SDK: translação em METROS, rotação euler "xyz". Usamos
    `create_head_pose(..., mm=True, degrees=True)` porque graus e milímetros são
    as unidades em que os limites acima fazem sentido para quem lê.
    """
    from reachy_mini.utils import create_head_pose

    return create_head_pose(
        x=lim.valor("x_mm", x_mm, -MAX_X_MM, MAX_X_MM),
        y=lim.valor("y_mm", y_mm, -MAX_Y_MM, MAX_Y_MM),
        z=lim.valor("z_mm", z_mm, -MAX_Z_MM, MAX_Z_MM),
        roll=lim.valor("roll_deg", roll_deg, -MAX_ROLL_DEG, MAX_ROLL_DEG),
        pitch=lim.valor("pitch_deg", pitch_deg, -MAX_PITCH_DEG, MAX_PITCH_DEG),
        yaw=lim.valor("yaw_deg", yaw_deg, -MAX_YAW_DEG, MAX_YAW_DEG),
        mm=True,
        degrees=True,
    )


def limitar_pose(
    lim: Limitador, pose: npt.NDArray[np.float64]
) -> npt.NDArray[np.float64]:
    """Reconstrói uma pose 4x4 já dentro do envelope.

    Existe porque `look_at_world_pose` / `look_at_image` do SDK calculam a pose
    a partir de um ponto no espaço, e nada impede esse ponto de exigir um giro
    de 80°. Decompor em euler, limitar e remontar é o único jeito de aplicar o
    mesmo envelope de `pose_cabeca` a uma pose que veio pronta.
    """
    from scipy.spatial.transform import Rotation as R

    p = np.asarray(pose, dtype=np.float64)
    if p.shape != (4, 4):
        raise ValueError("pose precisa ser 4x4")
    roll, pitch, yaw = R.from_matrix(p[:3, :3]).as_euler("xyz", degrees=True)
    x, y, z = p[:3, 3] * 1000.0  # metros → mm, que é a unidade dos limites
    return pose_cabeca(
        lim, yaw_deg=yaw, pitch_deg=pitch, roll_deg=roll, x_mm=x, y_mm=y, z_mm=z
    )


# Direção → (yaw, pitch) em fração do envelope. `intensity` multiplica isso.
# Nota de sinal, medida no robô: yaw positivo gira a cabeça para a ESQUERDA do
# robô (regra da mão direita em torno de z). "direita" para o usuário é o lado
# direito do robô, então leva yaw negativo.
DIRECOES: dict[str, tuple[float, float]] = {
    "left": (1.0, 0.0),
    "right": (-1.0, 0.0),
    "up": (0.0, -1.0),
    "down": (0.0, 1.0),
    "center": (0.0, 0.0),
    "up_left": (0.7, -0.7),
    "up_right": (-0.7, -0.7),
    "down_left": (0.7, 0.7),
    "down_right": (-0.7, 0.7),
}


def pose_direcao(
    lim: Limitador, direcao: str, intensidade: float
) -> npt.NDArray[np.float64]:
    """Pose para uma direção nomeada, escalada por `intensity` (0..1)."""
    fyaw, fpitch = DIRECOES[direcao]
    i = lim.intensidade(intensidade)
    return pose_cabeca(
        lim,
        yaw_deg=fyaw * MAX_YAW_DEG * i,
        pitch_deg=fpitch * MAX_PITCH_DEG * i,
    )


# ─── Nomes de app: allowlist de caracteres ───────────────────────────────────
# O nome vai para uma URL do daemon (`/api/apps/start-app/{app_name}`) e um dia
# pode vir de um modelo. Barra, ponto-ponto e espaço ficam de fora.
import re  # noqa: E402  (mantido junto da constante que o usa)

_APP_VALIDO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def nome_app_valido(nome: str) -> bool:
    """True se `nome` é seguro para ir na URL do daemon."""
    return bool(nome) and ".." not in nome and bool(_APP_VALIDO.match(nome))
