"""Compatibilidade. O comportamento do robô agora vive em `robo/comportamento.py`.

O antigo `Gestos` mandava `goto_target` direto no SDK a partir de uma thread
própria. Com a camada de ações no lugar, isso seria uma segunda mão mexendo no
robô sem passar pela fila — exatamente o que a fila existe para evitar.

Este módulo fica por uma versão para não quebrar import de fora do pacote (o
`ponte_garraia.py` legado tem a sua própria cópia e não depende daqui).
"""

from .robo.comportamento import Comportamento

# `Comportamento` tem `ouvindo()`, `pensando()`, `falando()` e `encerrar()` com a
# mesma assinatura — mas recebe o ControladorRobo, não o ReachyMini.
Gestos = Comportamento

__all__ = ["Comportamento", "Gestos"]
