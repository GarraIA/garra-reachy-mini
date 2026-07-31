"""Camada de controle do Reachy Mini.

Toda ação física do robô passa por aqui. Nenhum outro módulo do projeto chama o
SDK ou a API do daemon diretamente — é essa concentração que permite ter fila,
prioridade, limites, timeout, parada de emergência e estado consultável.

Ordem de dependência (de baixo para cima, sem ciclos):

    limites      constantes físicas e clamps; não importa nada do projeto
    barramento   eventos pub/sub; não importa nada do projeto
    daemon_api   cliente HTTP do daemon do robô
    backends     RoboBackend (Protocol) + BackendSdk / BackendSimulado
    catalogo     allowlist de ações, schemas e mapa de expressões
    acoes        ControladorRobo: fila, preempção, máquina de estados, e-stop
"""

from .acoes import ControladorRobo, EstadoControlador, ResultadoAcao
from .backends import BackendSdk, BackendSimulado, RoboBackend
from .barramento import Barramento, Evento
from .catalogo import CATALOGO, Acao
from .daemon_api import DaemonAPI, ErroDaemon

__all__ = [
    "CATALOGO",
    "Acao",
    "BackendSdk",
    "BackendSimulado",
    "Barramento",
    "ControladorRobo",
    "DaemonAPI",
    "ErroDaemon",
    "EstadoControlador",
    "Evento",
    "ResultadoAcao",
    "RoboBackend",
]
