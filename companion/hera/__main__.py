"""`python -m companion.hera` — a porta do dono para a conversa ao vivo.

    python -m companion.hera status
    python -m companion.hera liberar | desligar
    python -m companion.hera nivel conversa|web|completo
    python -m companion.hera limite <nome> <segundos>
    python -m companion.hera perfil
    python -m companion.hera token [--girar | --revogar]
    python -m companion.hera parear [--painel http://<ip-do-robo>:8042]

Liberar, subir de nível e aumentar teto só existem aqui, no computador. Pela
rede (o painel do robô) só se desliga e se reduz. Este comando nunca mostra o
segredo do dispositivo.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import sys
import time
from pathlib import Path

from .. import unidade
from . import credencial, livro, perfil, registro

UNIDADE = unidade.UNIDADE_HERA
CUSTO_POR_MINUTO_USD = 0.05      # preço público da API Live, cobrado por segundo


def soquete_da_ponte() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(base) / "garra-reachy" / "hera-bridge.sock"


def versao() -> str:
    try:
        from importlib import metadata
        return metadata.version("garra_reachy_mini")
    except Exception:
        return "dev"


class _ConexaoUnix(http.client.HTTPConnection):
    def __init__(self, caminho: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._caminho = caminho

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._caminho)


def _saude_da_ponte() -> dict | None:
    """O `/health` da ponte da Hera, ou `None` se ela não estiver no ar."""
    conexao = _ConexaoUnix(str(soquete_da_ponte()), timeout=3.0)
    try:
        conexao.request("GET", "/health")
        resposta = conexao.getresponse()
        return json.loads(resposta.read().decode("utf-8")) if resposta.status == 200 else None
    except (OSError, ValueError):
        return None
    finally:
        conexao.close()


def _reiniciar_ponte() -> None:
    """A ponte lê o perfil ao subir; se estiver rodando, sobe de novo com o novo."""
    if unidade.ativa(UNIDADE):
        unidade.agir("restart", UNIDADE)


def _regravar_perfil(reg: dict) -> Path:
    return perfil.gravar(perfil.montar(reg, versao=versao()))


def _tempo(segundos: float) -> str:
    return f"{int(segundos // 60)} min {int(segundos % 60):02d} s"


def _status() -> int:
    r = registro.Registro()
    reg = r.ler()
    uso = livro.Livro().uso(agora=time.time())
    saude = _saude_da_ponte()
    lim = reg["limits"]
    print(f"Hera no robô: {'liberada' if r.liberada() else 'desligada'}")
    print(f"  dispositivo: {reg['device']['id']} · nível: {reg['level']} · voz: {reg['voice']} · modelo: {reg['model']}")
    print(f"  tetos: {lim['session_max_s'] // 60} min por conversa · {lim['daily_s'] // 60} min por dia · "
          f"{lim['monthly_s'] // 60} min por mês · silêncio {lim['idle_close_s']} s")
    print(f"  uso: hoje {_tempo(uso['hoje_s'])} (≈ US$ {uso['hoje_s'] / 60 * CUSTO_POR_MINUTO_USD:.2f}) · "
          f"mês {_tempo(uso['mes_s'])} (≈ US$ {uso['mes_s'] / 60 * CUSTO_POR_MINUTO_USD:.2f}, estimado)")
    print(f"  credencial: {'criada' if credencial.existe() else 'não criada'}")
    if saude is None:
        print("  ponte da Hera: fora do ar")
    else:
        print(f"  ponte da Hera: no ar · canal {saude.get('channel')} · "
              f"chave da OpenAI {'disponível' if saude.get('available') else 'ausente'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m companion.hera",
                                     description="A conversa ao vivo (Hera) no robô.")
    sub = parser.add_subparsers(dest="comando", required=True)
    sub.add_parser("status")
    sub.add_parser("liberar")
    sub.add_parser("desligar")
    sub.add_parser("nivel").add_argument("nivel")
    limite = sub.add_parser("limite")
    limite.add_argument("nome")
    limite.add_argument("valor")
    sub.add_parser("perfil")
    token = sub.add_parser("token")
    grupo = token.add_mutually_exclusive_group()
    grupo.add_argument("--girar", action="store_true")
    grupo.add_argument("--revogar", action="store_true")
    parear = sub.add_parser("parear", help="grava no robô o segredo e o nome do dispositivo")
    parear.add_argument("--painel", help="padrão: o robô que o vigia lembra")
    args = parser.parse_args(argv)

    r = registro.Registro()
    try:
        if args.comando == "status":
            return _status()
        if args.comando in ("liberar", "desligar"):
            reg = r.mudar({"enabled": args.comando == "liberar"}, local=True)
            if reg["enabled"]:
                credencial.segredo()
                _regravar_perfil(reg)
            print(f"Hera no robô: {'liberada' if reg['enabled'] else 'desligada'}.")
            return 0
        if args.comando == "nivel":
            reg = r.mudar({"level": args.nivel}, local=True)
            _regravar_perfil(reg)
            _reiniciar_ponte()
            print(f"Nível: {reg['level']}. A conversa do nível anterior fica guardada no Hermes.")
            return 0
        if args.comando == "limite":
            try:
                valor = int(args.valor)
            except ValueError:
                raise ValueError(f"{args.nome}: o valor precisa ser um número inteiro") from None
            reg = r.mudar({"limits": {args.nome: valor}}, local=True)
            print(f"{args.nome} = {reg['limits'][args.nome]}")
            return 0
        if args.comando == "perfil":
            print(f"Perfil do canal gravado em {_regravar_perfil(r.ler())}")
            _reiniciar_ponte()
            return 0
        if args.comando == "parear":
            from .. import reachy
            painel = args.painel or reachy.painel_lembrado()
            if not painel:
                print("não achei o robô; passe --painel http://<ip-do-robo>:8042", file=sys.stderr)
                return 2
            reg = r.ler()
            try:
                # Pela mesma gravação assinada de sempre: o robô pareado só aceita
                # isto de quem conhece a credencial dele. O segredo vai no corpo,
                # pela rede de casa, uma vez; depois disso só circulam provas.
                reachy._gravar(painel, {"hera_token": credencial.segredo(),
                                        "hera_device": reg["device"]["id"]})
            except reachy.ErroReachy as erro:
                print(f"o robô recusou: {erro}", file=sys.stderr)
                return 1
            print(f"Robô em {painel} pareado com a Hera (dispositivo {reg['device']['id']}).")
            return 0
        if args.comando == "token":
            if args.revogar:
                credencial.revogar()
                print("Credencial apagada. O robô não abre mais conversa até ser pareado de novo.")
            elif args.girar:
                credencial.girar()
                print("Credencial trocada. Pareie o robô de novo para ele voltar a abrir conversa.")
            else:
                credencial.segredo()
                print("Credencial pronta (o valor não é mostrado; o pareamento a leva ao robô).")
            return 0
    except ValueError as erro:
        print(f"recusado: {erro}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
