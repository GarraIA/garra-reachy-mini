"""Camada web do app: painel do Reachy, API REST/WS e ponte de chat.

Nada aqui toca o robô diretamente — tudo passa pelo `ControladorRobo` de
`garra_reachy_mini.robo`.
"""

from .api import ContextoWeb, montar, preparar
from .camera import FrameHub, Quadro
from .chat import ErroChat, PonteChat
from .seguranca import Politica, resolver_politica

__all__ = [
    "ContextoWeb",
    "ErroChat",
    "FrameHub",
    "Politica",
    "PonteChat",
    "Quadro",
    "montar",
    "preparar",
    "resolver_politica",
]
