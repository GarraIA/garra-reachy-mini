"""Agentes do robô, pela linha de comando — o que só o dono faz, e só aqui.

    python -m companion.agentes                       # lista, com a saúde de cada um
    python -m companion.agentes usar hermes           # escolhe o agente ativo
    python -m companion.agentes testar pi             # uma conversa de verdade
    python -m companion.agentes opcao hermes tools completo
    python -m companion.agentes reserva garra         # ou: reserva nenhum
    python -m companion.agentes adicionar receita.json
    python -m companion.agentes remover meu-agente

`adicionar` lê uma receita em JSON (ver `comando.py` para linha de comando e
`http_openai.py` para servidor HTTP). É por aqui — e não pelo painel do robô —
que entra um agente de linha de comando, e que se libera um agente para usar
todas as ferramentas: o painel atende a rede da casa, e este comando só roda
para quem já está sentado neste computador.

Não precisa reiniciar nada: o companion relê `agentes.json` quando ele muda.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .. import agente as identidade
from .base import ErroDoAgente
from .registro import Registro

USO = __doc__.split("\n\n")[1]


def _registro() -> Registro:
    from .. import ponte, servidor
    return Registro(chave_gateway=servidor._chave_do_gateway(),
                    modelo_do_garra=ponte.modelo_configurado, identidade=identidade.ler)


def _listar(r: Registro) -> None:
    d = r.listar()
    for a in d["agents"]:
        marca = "●" if a["active"] else " "
        estado = "disponível" if a["available"] else f"INDISPONÍVEL ({a['health']['code']})"
        opcoes = ", ".join(f"{o['key']}={o['value'] or 'automático'}" for o in a["options"])
        print(f" {marca} {a['id']:<10} {a['name']:<24} {a['interface']:<8} {estado:<28} "
              f"{a['safety']:<13} {opcoes}")
    print(f"\nativo: {d['active']} ({'escolhido pelo dono' if d['explicit'] else 'padrão'})"
          f" · reserva: {d['fallback'] or 'nenhum'} · arquivo: {r.arquivo}")


def main(argv: list[str]) -> int:
    r = _registro()
    try:
        if not argv or argv[0] == "listar":
            _listar(r)
        elif argv[0] == "usar" and len(argv) == 2:
            r.escolher(argv[1])
            _listar(r)
        elif argv[0] == "testar" and len(argv) == 2:
            print(json.dumps(r.testar(argv[1]), ensure_ascii=False, indent=1))
        elif argv[0] == "opcao" and len(argv) == 4:
            r.configurar(argv[1], {argv[2]: argv[3]}, local=True)
            _listar(r)
        elif argv[0] == "reserva" and len(argv) == 2:
            r.definir_reserva(None if argv[1] in ("nenhum", "none", "") else argv[1])
            _listar(r)
        elif argv[0] == "adicionar" and len(argv) == 2:
            definicao = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
            novo = r.adicionar(definicao, local=True)
            print(f"adicionado: {novo.id} ({novo.nome}). Teste com: "
                  f"python -m companion.agentes testar {novo.id}")
        elif argv[0] == "remover" and len(argv) == 2:
            r.remover(argv[1])
            _listar(r)
        else:
            print(USO)
            return 2
    except ErroDoAgente as e:
        print(f"recusado: {e.detalhe or e.codigo}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as e:
        print(f"erro: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
