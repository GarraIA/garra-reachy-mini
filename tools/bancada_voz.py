#!/usr/bin/env python3
"""Bancada de voz: fala com o robô pelas caixas do PC e confere o que voltou.

O teste de ponta a ponta que dispensa uma pessoa na sala. As caixas de som
deste PC dizem a frase; o microfone do ROBÔ a ouve; o robô transcreve, consulta
o agente e responde pelo alto-falante dele; o microfone do PC grava a resposta.
A prova vem de dois lugares independentes:

  * o barramento de eventos do robô (`/api/robot/events`) — o que ele ouviu,
    se a frase de ativação casou, o que respondeu e quanto demorou;
  * a gravação do microfone do PC, transcrita — se o som saiu MESMO.

Uso:
    bancada_voz.py dizer "Fala Garra, que horas são?"
    bancada_voz.py roteiro roteiro.json          # várias falas, com placar
    bancada_voz.py ouvir 5                       # só grava e transcreve
    bancada_voz.py robo-diz "Fala Garra, oi"     # o robô fala; ele se ativa sozinho?

Precisa do servidor de voz do PC (TTS da fala de teste e STT da gravação), de
`pw-play`/`pw-record` (PipeWire) e do robô com o app no ar. Nada fica gravado:
os áudios vivem numa pasta temporária apagada ao sair.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import unicodedata
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import requests

SR_FALA = 24000      # o que o Chatterbox gera
SR_ESCUTA = 16000    # o que o Whisper quer
EVENTOS = ("voice.wake.activated", "voice.wake.rejected", "voice.wake.near_miss",
           "voice.turn.started", "voice.turn.completed", "voice.turn.cancelled",
           "voice.error", "chat.message", "chat.error", "voice.state")


def _token() -> str | None:
    try:
        return (Path("~/.config/garra-reachy/voz.token").expanduser()
                .read_text(encoding="utf-8").strip() or None)
    except OSError:
        return None


def _wav(caminho: Path, onda: np.ndarray, sr: int) -> None:
    pcm = (np.clip(onda, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(caminho), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def _ler_wav(caminho: Path) -> np.ndarray:
    with wave.open(str(caminho), "rb") as w:
        bruto = w.readframes(w.getnframes())
        canais = w.getnchannels()
    onda = np.frombuffer(bruto, dtype="<i2").astype(np.float32) / 32768.0
    return onda.reshape(-1, canais).mean(axis=1) if canais > 1 else onda


def palavras(texto: str) -> list[str]:
    sem = unicodedata.normalize("NFD", texto.lower())
    sem = "".join(c if c.isalnum() or c.isspace() else " " for c in sem
                  if unicodedata.category(c) != "Mn")
    return sem.split()


def semelhanca(esperado: str, obtido: str) -> float:
    """Fração das palavras de `esperado` que aparecem em `obtido`."""
    alvo = palavras(esperado)
    if not alvo:
        return 0.0
    achadas = set(palavras(obtido))
    return sum(p in achadas for p in alvo) / len(alvo)


class Bancada:
    def __init__(self, robo: str, voz: str, pasta: Path) -> None:
        self.robo, self.voz, self.pasta = robo.rstrip("/"), voz.rstrip("/"), pasta
        tok = _token()
        self.cab = {"Authorization": f"Bearer {tok}"} if tok else {}
        self._n = 0
        self._falas: dict[str, Path] = {}

    # ── PC ───────────────────────────────────────────────────────────────────
    def sintetizar(self, texto: str, tentativas: int = 5) -> Path:
        """A fala de teste — conferida antes de ser tocada.

        A síntese às vezes sai com um trecho a mais ou uma palavra engolida, e
        um estímulo com defeito mede a síntese, não o robô. Cada tentativa é
        transcrita aqui mesmo, sem passar por caixa nem microfone; vale a
        primeira que voltar igual ao texto e sem sobra. O resultado fica
        guardado: a mesma frase é sempre o mesmo áudio dentro de uma rodada.
        """
        if texto in self._falas:
            return self._falas[texto]
        melhor: tuple[float, np.ndarray] | None = None
        for _ in range(tentativas):
            r = requests.post(f"{self.voz}/falar", json={"texto": texto, "sr": SR_FALA},
                              headers=self.cab, timeout=120)
            r.raise_for_status()
            onda = np.frombuffer(r.content, dtype=np.float32)
            em_16k = onda[::3] if SR_FALA == 3 * SR_ESCUTA else onda
            # 24 kHz → 16 kHz por interpolação: basta para o Whisper conferir.
            if SR_FALA != 3 * SR_ESCUTA:
                n = int(onda.size * SR_ESCUTA / SR_FALA)
                em_16k = np.interp(np.linspace(0, onda.size - 1, n),
                                   np.arange(onda.size), onda).astype(np.float32)
            ouvido = self.transcrever(em_16k)
            nota = semelhanca(texto, ouvido)
            sobra = len(palavras(ouvido)) - len(palavras(texto))
            if sobra > 1:
                nota -= 0.5
            if melhor is None or nota > melhor[0]:
                melhor = (nota, onda)
            if nota >= 0.99:
                break
        self._n += 1
        arquivo = self.pasta / f"fala_{self._n}.wav"
        _wav(arquivo, melhor[1], SR_FALA)
        self._falas[texto] = arquivo
        return arquivo

    def tocar(self, arquivo: Path) -> None:
        subprocess.run(["pw-play", str(arquivo)], check=True, timeout=120)

    def gravar(self, segundos: float) -> subprocess.Popen:
        self._n += 1
        self._gravando = self.pasta / f"escuta_{self._n}.wav"
        return subprocess.Popen(
            ["pw-record", "--rate", str(SR_ESCUTA), "--channels", "1",
             str(self._gravando)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def parar(self, gravacao: subprocess.Popen) -> np.ndarray:
        gravacao.terminate()
        try:
            gravacao.wait(timeout=5)
        except subprocess.TimeoutExpired:
            gravacao.kill()
        try:
            return _ler_wav(self._gravando)
        except (OSError, wave.Error, EOFError):
            return np.empty(0, dtype=np.float32)

    def transcrever(self, onda: np.ndarray) -> str:
        if onda.size < SR_ESCUTA // 4:
            return ""
        r = requests.post(f"{self.voz}/transcrever", params={"sr": SR_ESCUTA},
                          data=onda.astype(np.float32).tobytes(),
                          headers=self.cab, timeout=120)
        r.raise_for_status()
        return (r.json().get("texto") or "").strip()

    # ── robô ─────────────────────────────────────────────────────────────────
    def eventos(self, desde: datetime) -> list[dict]:
        r = requests.get(f"{self.robo}/api/robot/events",
                         params={"limite": 300, "tipos": ",".join(EVENTOS)}, timeout=8)
        r.raise_for_status()
        saida = []
        for e in r.json().get("events", []):
            try:
                quando = datetime.fromisoformat(e["timestamp"])
            except (KeyError, ValueError):
                continue
            if quando >= desde:
                saida.append(e)
        return saida

    def estado(self) -> dict:
        r = requests.get(f"{self.robo}/api/robot/status", timeout=8)
        r.raise_for_status()
        return r.json()

    # ── um turno inteiro ─────────────────────────────────────────────────────
    def dizer(self, texto: str, espera_s: float = 45.0, quieto: bool = False) -> dict:
        """Diz `texto` ao robô e devolve o que os dois lados registraram."""
        arquivo = self.sintetizar(texto)
        inicio = datetime.now(timezone.utc)
        gravacao = self.gravar(espera_s)
        t0 = time.monotonic()
        self.tocar(arquivo)
        dito_em = time.monotonic() - t0
        resultado: dict = {"dito": texto, "ativou": False, "ouvido": None,
                           "resposta": None, "som": None, "agente": None,
                           "cerebro_ms": None, "total_ms": None, "rejeitado": None,
                           "quase": None, "erro": None}
        fim = time.monotonic() + espera_s
        terminou_em = None
        while time.monotonic() < fim:
            time.sleep(0.5)
            try:
                evs = self.eventos(inicio)
            except requests.RequestException as e:
                resultado["erro"] = f"robô inacessível: {type(e).__name__}"
                break
            for e in evs:
                tipo = e["type"]
                if tipo == "voice.wake.activated":
                    resultado["ativou"] = True
                    resultado["casamento"] = e.get("match")
                elif tipo == "voice.wake.rejected":
                    resultado["rejeitado"] = e.get("reason")
                elif tipo == "voice.wake.near_miss":
                    resultado["quase"] = {k: e.get(k) for k in
                                          ("heard", "distance", "confidence", "second_listen")}
                elif tipo == "chat.message" and e.get("source") == "voz":
                    resultado["ouvido"] = e.get("content")
                elif tipo == "chat.message" and e.get("role") == "assistant":
                    resultado["resposta"] = e.get("content")
                    resultado["agente"] = e.get("agent") or e.get("brain")
                elif tipo == "voice.turn.completed":
                    resultado["cerebro_ms"] = e.get("brain_ms")
                    resultado["total_ms"] = e.get("total_ms")
                    terminou_em = terminou_em or time.monotonic()
                elif tipo in ("voice.error", "chat.error"):
                    resultado["erro"] = e.get("message") or e.get("code") or tipo
            if terminou_em is not None and time.monotonic() - terminou_em > 1.0:
                break
            # Nada aconteceu 8 s depois de a fala acabar: o robô não reagiu.
            if (not resultado["ativou"] and resultado["ouvido"] is None
                    and time.monotonic() - t0 > dito_em + 8.0):
                break
        onda = self.parar(gravacao)
        # Só o que veio DEPOIS da fala de teste: antes disso o microfone do PC
        # estava ouvindo as próprias caixas.
        depois = onda[int(dito_em * SR_ESCUTA):]
        if resultado["resposta"]:
            resultado["som"] = self.transcrever(depois)
            resultado["som_bate"] = round(semelhanca(resultado["resposta"], resultado["som"]), 2)
        if not quieto:
            print(json.dumps(resultado, ensure_ascii=False, indent=1))
        return resultado


def _roteiro(b: Bancada, arquivo: Path) -> int:
    """Roda um roteiro: lista de {"dizer", "espera": "responde"|"ignora", "pausa_s"}."""
    passos = json.loads(arquivo.read_text(encoding="utf-8"))
    certos = 0
    for i, passo in enumerate(passos, 1):
        r = b.dizer(passo["dizer"], espera_s=float(passo.get("limite_s", 45)), quieto=True)
        respondeu = bool(r["resposta"])
        ok = respondeu if passo.get("espera", "responde") == "responde" else not respondeu
        certos += ok
        print(f"[{i:02d}] {'PASSOU' if ok else 'FALHOU'} | dito: {passo['dizer']!r} | "
              f"ouvido: {r['ouvido']!r} | resposta: {(r['resposta'] or '')[:70]!r} | "
              f"som bate: {r.get('som_bate')} | {r['total_ms']} ms"
              + (f" | quase: {r['quase']}" if r["quase"] else ""), flush=True)
        time.sleep(float(passo.get("pausa_s", 3)))
    print(f"placar: {certos}/{len(passos)}")
    return 0 if certos == len(passos) else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--robo", default="http://reachy-mini.local:8042")
    p.add_argument("--voz", default="http://127.0.0.1:8123")
    p.add_argument("--volume", type=float, default=None,
                   help="volume das caixas do PC durante o teste (0 a 1); o "
                        "anterior é devolvido ao sair")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dizer")
    d.add_argument("texto")
    d.add_argument("--limite", type=float, default=45.0)
    r = sub.add_parser("roteiro")
    r.add_argument("arquivo", type=Path)
    o = sub.add_parser("ouvir")
    o.add_argument("segundos", type=float)
    rd = sub.add_parser("robo-diz")
    rd.add_argument("texto")
    args = p.parse_args()

    anterior = None
    if args.volume is not None:
        lido = subprocess.run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"],
                              capture_output=True, text=True).stdout.split()
        anterior = lido[1] if len(lido) > 1 else None
        subprocess.run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@",
                        f"{min(1.0, max(0.0, args.volume)):.2f}"], check=False)
    try:
        return _rodar(args)
    finally:
        if anterior is not None:
            subprocess.run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", anterior],
                           check=False)


def _rodar(args) -> int:
    with tempfile.TemporaryDirectory(prefix="bancada-voz-") as pasta:
        b = Bancada(args.robo, args.voz, Path(pasta))
        if args.cmd == "dizer":
            res = b.dizer(args.texto, espera_s=args.limite)
            return 0 if res["resposta"] else 1
        if args.cmd == "roteiro":
            return _roteiro(b, args.arquivo)
        if args.cmd == "ouvir":
            g = b.gravar(args.segundos)
            time.sleep(args.segundos)
            onda = b.parar(g)
            nivel = float(np.sqrt(np.mean(onda ** 2))) if onda.size else 0.0
            print(json.dumps({"segundos": round(onda.size / SR_ESCUTA, 1),
                              "nivel_rms": round(nivel, 4),
                              "texto": b.transcrever(onda)}, ensure_ascii=False))
            return 0
        if args.cmd == "robo-diz":
            # O robô diz a própria frase de ativação. Ele NÃO pode se ativar.
            inicio = datetime.now(timezone.utc)
            g = b.gravar(20)
            resp = requests.post(f"{b.robo}/api/chat/falar", json={"texto": args.texto},
                                 timeout=60)
            time.sleep(12)
            onda = b.parar(g)
            evs = b.eventos(inicio)
            ativou = any(e["type"] == "voice.wake.activated" for e in evs)
            print(json.dumps({"http": resp.status_code, "o_pc_ouviu": b.transcrever(onda),
                              "robo_se_ativou": ativou,
                              "eventos": sorted({e["type"] for e in evs})},
                             ensure_ascii=False, indent=1))
            return 1 if ativou else 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
