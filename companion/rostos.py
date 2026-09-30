"""Família: perfis consentidos e reconhecimento de rostos, tudo neste PC.

Isto NÃO é o rastreio de rosto do robô (que só acha "um rosto" para olhar
para ele) e NÃO passa pelo modelo de visão (que nunca é convidado a adivinhar
nome). É outra coisa, mais estreita: comparar o vetor do rosto de agora com os
vetores dos PERFIS que a família cadastrou, de propósito, um a um.

Regras que este módulo garante sozinho, sem depender de quem o chama:

- perfil só nasce com consentimento explícito de quem é cadastrado; menor só
  com o responsável registrado;
- de cada amostra fica só o VETOR (128 números); a imagem morre na hora;
- os vetores ficam cifrados (Fernet) em `~/.config/garra-reachy/rostos/`, com
  a chave 0600 ao lado, e a pasta 0700 — fora do repositório, sem backup
  automático, apagáveis pelo painel;
- "desconhecido" é resposta legítima: abaixo do limiar, ou quando os dois
  melhores candidatos estão perto demais (senão seria chute);
- reconhecer alguém nunca autoriza nada — é contexto para a conversa, só.

O detector/extrator (YuNet + SFace, OpenCV) vive em `tools/servidor_rostos.py`
porque o venv do companion não tem OpenCV. Aqui entra qualquer `motor` com
`detectar(jpeg) -> list[Rosto]`, o que deixa tudo testável sem modelo.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from cryptography.fernet import Fernet, InvalidToken

PASTA_PADRAO = Path.home() / ".config" / "garra-reachy" / "rostos"

# Teto de amostras por perfil: 3–5 bastam para o SFace, e mais do que isso só
# aumenta a chance de entrar amostra ruim.
AMOSTRAS_MAX = 5
# Qualidade mínima de uma amostra. Abaixo disso o vetor sai ruim e o perfil
# inteiro passa a reconhecer mal — melhor recusar na hora, com o motivo.
TAMANHO_MINIMO_PX = 80          # menor lado da caixa do rosto
NITIDEZ_MINIMA = 30.0           # variância do Laplaciano no recorte
BRILHO_MINIMO = 40.0            # média de cinza no recorte (0–255)
BRILHO_MAXIMO = 220.0
# Cosseno mínimo para "é fulano". O SFace documenta 0,363; aqui é mais duro
# de propósito: errar o nome é pior do que dizer "não sei".
LIMIAR_SEMELHANCA = 0.45
# O melhor tem de ganhar do segundo por esta margem, senão é "desconhecido".
MARGEM_MINIMA = 0.10
NOME_MAX = 40
_NOME = re.compile(r"^[^\W\d_][\w .'\-]{0,39}$")

MOTIVOS_DA_AMOSTRA = ("no_face", "many_faces", "too_small", "blurry", "too_dark", "too_bright",
                      "no_embedding", "enough_samples")
MOTIVOS_DO_DESCONHECIDO = ("no_embedding", "no_profiles", "below_threshold", "ambiguous")


class ErroRostos(RuntimeError):
    """`codigo` é estável (vai ao painel); `detalhe` é para gente."""

    def __init__(self, codigo: str, detalhe: str = "") -> None:
        super().__init__(detalhe or codigo)
        self.codigo = codigo
        self.detalhe = detalhe


@dataclass(frozen=True)
class Rosto:
    caixa: tuple[int, int, int, int]      # x, y, largura, altura na imagem
    pontuacao: float                      # confiança do detector
    embedding: np.ndarray | None          # vetor do SFace; None se a extração falhou
    nitidez: float                        # variância do Laplaciano no recorte
    brilho: float                         # média de cinza no recorte


def _agora_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _consentimento(dado) -> dict:
    """Normaliza o consentimento vindo do painel; recusa o que não é explícito."""
    if not isinstance(dado, dict) or dado.get("accepted") is not True:
        raise ErroRostos("consent_required", "o consentimento tem de ser dado explicitamente")
    por = str(dado.get("by") or "").strip()
    if not por:
        raise ErroRostos("consent_required", "falta quem consentiu")
    menor = bool(dado.get("minor"))
    responsavel = str(dado.get("guardian") or "").strip() or None
    if menor and not responsavel:
        raise ErroRostos("guardian_required", "menor de idade só com o responsável registrado")
    return {"accepted": True, "by": por[:NOME_MAX], "minor": menor, "guardian": responsavel,
            "at": _agora_iso()}


def _nome(dado) -> str:
    nome = str(dado or "").strip()
    if not _NOME.match(nome):
        raise ErroRostos("invalid_name", "nome: letras, espaços, ponto, hífen ou apóstrofo; até 40")
    return nome


def _unitario(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def _escrever(caminho: Path, dados: bytes) -> None:
    """Grava atômico e só para o dono, sem depender do umask de quem roda."""
    tmp = caminho.with_name(caminho.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(dados)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, caminho)


class Base:
    """Perfis (metadados em claro, sem biometria) + vetores cifrados por perfil."""

    def __init__(self, pasta: Path | str = PASTA_PADRAO) -> None:
        self.pasta = Path(pasta)
        self.pasta.mkdir(parents=True, exist_ok=True)
        os.chmod(self.pasta, 0o700)
        self._fernet = Fernet(self._chave())

    # ─── arquivos ────────────────────────────────────────────────────────────
    def _chave(self) -> bytes:
        caminho = self.pasta / "chave"
        if caminho.exists():
            os.chmod(caminho, 0o600)
            return caminho.read_bytes().strip()
        chave = Fernet.generate_key()
        _escrever(caminho, chave)
        return chave

    def _caminho_metadados(self) -> Path:
        return self.pasta / "perfis.json"

    def _caminho_vetores(self, perfil_id: str) -> Path:
        return self.pasta / f"{perfil_id}.emb"

    def _ler(self) -> list[dict]:
        caminho = self._caminho_metadados()
        if not caminho.exists():
            return []
        try:
            dados = json.loads(caminho.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return list(dados.get("perfis") or [])

    def _gravar(self, perfis: list[dict]) -> None:
        _escrever(self._caminho_metadados(), json.dumps({"perfis": perfis}, ensure_ascii=False, indent=1).encode())

    def _ler_vetores(self, perfil_id: str) -> list[np.ndarray] | None:
        """Lista de vetores; [] sem amostras; None se existem mas não abrem."""
        caminho = self._caminho_vetores(perfil_id)
        if not caminho.exists():
            return []
        try:
            claro = self._fernet.decrypt(caminho.read_bytes())
            return [_unitario(np.asarray(v, dtype=np.float32)) for v in json.loads(claro)["v"]]
        except (InvalidToken, OSError, ValueError, KeyError, TypeError):
            return None

    def _gravar_vetores(self, perfil_id: str, vetores: list[np.ndarray]) -> None:
        claro = json.dumps({"v": [[float(x) for x in v] for v in vetores]}).encode()
        _escrever(self._caminho_vetores(perfil_id), self._fernet.encrypt(claro))

    # ─── perfis ──────────────────────────────────────────────────────────────
    def _publico(self, p: dict) -> dict:
        legivel = p["samples"] == 0 or self._ler_vetores(p["id"]) is not None
        return {"id": p["id"], "name": p["name"], "active": bool(p["active"]), "samples": int(p["samples"]),
                "consent": dict(p["consent"]), "created_at": p["created_at"], "samples_readable": legivel}

    def perfis(self) -> list[dict]:
        return [self._publico(p) for p in self._ler()]

    def _achar(self, perfis: list[dict], perfil_id: str) -> dict:
        for p in perfis:
            if p["id"] == perfil_id:
                return p
        raise ErroRostos("not_found", "perfil não existe")

    def perfil(self, perfil_id: str) -> dict:
        return self._publico(self._achar(self._ler(), perfil_id))

    def criar(self, nome, consentimento) -> dict:
        consent = _consentimento(consentimento)
        perfis = self._ler()
        p = {"id": uuid.uuid4().hex[:12], "name": _nome(nome), "active": True, "samples": 0,
             "consent": consent, "created_at": _agora_iso()}
        perfis.append(p)
        self._gravar(perfis)
        return self._publico(p)

    def apagar(self, perfil_id: str) -> None:
        perfis = self._ler()
        self._achar(perfis, perfil_id)
        self._caminho_vetores(perfil_id).unlink(missing_ok=True)
        self._gravar([p for p in perfis if p["id"] != perfil_id])

    def _ativo(self, perfil_id: str, ativo: bool) -> dict:
        perfis = self._ler()
        p = self._achar(perfis, perfil_id)
        p["active"] = ativo
        self._gravar(perfis)
        return self._publico(p)

    def ativar(self, perfil_id: str) -> dict:
        return self._ativo(perfil_id, True)

    def desativar(self, perfil_id: str) -> dict:
        return self._ativo(perfil_id, False)

    def limpar_amostras(self, perfil_id: str) -> int:
        perfis = self._ler()
        p = self._achar(perfis, perfil_id)
        antes = int(p["samples"])
        self._caminho_vetores(perfil_id).unlink(missing_ok=True)
        p["samples"] = 0
        self._gravar(perfis)
        return antes

    def guardar_vetor(self, perfil_id: str, vetor: np.ndarray) -> int:
        """Acrescenta um vetor ao perfil; devolve quantos ficaram."""
        perfis = self._ler()
        p = self._achar(perfis, perfil_id)
        atuais = self._ler_vetores(perfil_id)
        if atuais is None:
            # Os antigos não abrem com a chave de agora: recomeça o perfil em vez
            # de misturar vetores que ninguém consegue ler.
            atuais = []
        atuais.append(_unitario(vetor))
        self._gravar_vetores(perfil_id, atuais)
        p["samples"] = len(atuais)
        self._gravar(perfis)
        return len(atuais)

    def galeria(self) -> list[tuple[dict, list[np.ndarray]]]:
        """Perfis ativos com vetores legíveis — o que o reconhecimento compara."""
        saida = []
        for p in self._ler():
            if not p["active"] or not p["samples"]:
                continue
            vetores = self._ler_vetores(p["id"])
            if vetores:
                saida.append((self._publico(p), vetores))
        return saida


def _qualidade(rostos: list[Rosto]) -> str | None:
    if not rostos:
        return "no_face"
    if len(rostos) > 1:
        return "many_faces"
    [r] = rostos
    if min(r.caixa[2], r.caixa[3]) < TAMANHO_MINIMO_PX:
        return "too_small"
    if r.nitidez < NITIDEZ_MINIMA:
        return "blurry"
    if r.brilho < BRILHO_MINIMO:
        return "too_dark"
    if r.brilho > BRILHO_MAXIMO:
        return "too_bright"
    if r.embedding is None:
        return "no_embedding"
    return None


class Reconhecedor:
    def __init__(self, base: Base, motor, limiar: float = LIMIAR_SEMELHANCA, margem: float = MARGEM_MINIMA) -> None:
        self.base = base
        self.motor = motor
        self.limiar = float(limiar)
        self.margem = float(margem)

    def cadastrar(self, perfil_id: str, jpeg: bytes) -> dict:
        """Uma amostra: exige UM rosto bom; guarda o vetor e descarta a imagem."""
        perfil = self.base.perfil(perfil_id)
        if perfil["samples"] >= AMOSTRAS_MAX:
            return {"ok": False, "reason": "enough_samples", "samples": perfil["samples"]}
        rostos = self.motor.detectar(jpeg)
        motivo = _qualidade(rostos)
        if motivo:
            return {"ok": False, "reason": motivo, "samples": perfil["samples"]}
        [r] = rostos
        n = self.base.guardar_vetor(perfil_id, r.embedding)
        return {"ok": True, "samples": n,
                "quality": {"size_px": int(min(r.caixa[2], r.caixa[3])), "sharpness": float(r.nitidez),
                            "brightness": float(r.brilho)}}

    def reconhecer(self, jpeg: bytes) -> list[dict]:
        """Um item por rosto na imagem; nada é gravado."""
        galeria = self.base.galeria()
        vistos = []
        for r in self.motor.detectar(jpeg):
            visto = {"box": [int(x) for x in r.caixa], "known": False, "profile_id": None, "name": None,
                     "score": None, "reason": None}
            if r.embedding is None:
                visto["reason"] = "no_embedding"
            elif not galeria:
                visto["reason"] = "no_profiles"
            else:
                e = _unitario(r.embedding)
                pontos = sorted(((max(float(e @ v) for v in vetores), p) for p, vetores in galeria),
                                key=lambda par: par[0], reverse=True)
                melhor, perfil = pontos[0]
                segundo = pontos[1][0] if len(pontos) > 1 else -1.0
                visto["score"] = round(melhor, 4)
                if melhor < self.limiar:
                    visto["reason"] = "below_threshold"
                elif melhor - segundo < self.margem:
                    visto["reason"] = "ambiguous"
                else:
                    visto.update(known=True, profile_id=perfil["id"], name=perfil["name"])
            vistos.append(visto)
        return vistos
