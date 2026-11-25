"""O livro das conversas ao vivo: o que foi gasto, e os tetos.

Cada conversa custa por segundo, e o site nunca anotou nenhuma. Aqui cada
abertura vira uma linha — quando, quanto durou, por que acabou — e é sobre estas
linhas que os tetos são aplicados ANTES de abrir a próxima. Nenhuma palavra do
que foi dito entra aqui.

SQLite num arquivo 0600; uma conexão curta por operação, para servir a qualquer
thread sem estado compartilhado.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

# Chamado sem ninguém falar depois: o sinal de ativação falsa.
VAZIAS = ("no_speech",)
VAZIAS_SEGUIDAS = 3
VAZIAS_JANELA_S = 600.0
SUSPENSAO_S = 600.0
_COLUNAS = ("id", "dispositivo", "nivel", "como", "inicio", "fim", "segundos",
            "motivo", "delegacoes", "sessao_openai")


def pasta_dados() -> Path:
    return Path(os.environ.get("GARRA_REACHY_DADOS", "~/.local/share/garra-reachy")).expanduser()


class Livro:
    def __init__(self, arquivo: Path | None = None) -> None:
        self.arquivo = arquivo or pasta_dados() / "hera" / "sessoes.sqlite"

    def _abrir_banco(self) -> sqlite3.Connection:
        novo = not self.arquivo.exists()
        if novo:
            self.arquivo.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.close(os.open(self.arquivo, os.O_WRONLY | os.O_CREAT, 0o600))
        banco = sqlite3.connect(self.arquivo, timeout=5.0)
        banco.row_factory = sqlite3.Row
        banco.execute(
            "CREATE TABLE IF NOT EXISTS sessoes ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, dispositivo TEXT NOT NULL, nivel TEXT NOT NULL, "
            "como TEXT NOT NULL, inicio REAL NOT NULL, fim REAL, segundos REAL, motivo TEXT, "
            "delegacoes INTEGER NOT NULL DEFAULT 0, sessao_openai TEXT)")
        return banco

    # ── escrita ──────────────────────────────────────────────────────────────
    def abrir(self, *, dispositivo: str, nivel: str, como: str, agora: float,
              sessao_openai: str | None = None) -> int:
        with closing(self._abrir_banco()) as banco, banco:
            cursor = banco.execute(
                "INSERT INTO sessoes (dispositivo, nivel, como, inicio, sessao_openai) VALUES (?,?,?,?,?)",
                (dispositivo, nivel, como, agora, sessao_openai))
            return int(cursor.lastrowid)

    def anotar(self, sid: int, *, sessao_openai: str) -> None:
        with closing(self._abrir_banco()) as banco, banco:
            banco.execute("UPDATE sessoes SET sessao_openai = ? WHERE id = ?", (sessao_openai, sid))

    def fechar(self, sid: int, *, segundos: float, motivo: str, agora: float,
               delegacoes: int = 0) -> None:
        with closing(self._abrir_banco()) as banco, banco:
            banco.execute(
                "UPDATE sessoes SET fim = ?, segundos = ?, motivo = ?, delegacoes = ? "
                "WHERE id = ? AND fim IS NULL",
                (agora, max(0.0, float(segundos)), motivo, int(delegacoes), sid))

    def fechar_orfas(self, *, agora: float) -> list[dict[str, Any]]:
        """Fecha o que ficou aberto (processo que caiu) e devolve para o desligamento na OpenAI."""
        with closing(self._abrir_banco()) as banco, banco:
            orfas = [dict(l) for l in banco.execute("SELECT * FROM sessoes WHERE fim IS NULL")]
            banco.execute(
                "UPDATE sessoes SET fim = ?, segundos = MAX(0, ? - inicio), motivo = 'orphan_reaped' "
                "WHERE fim IS NULL", (agora, agora))
        return orfas

    # ── leitura ──────────────────────────────────────────────────────────────
    def sessoes(self, limite: int = 50) -> list[dict[str, Any]]:
        with closing(self._abrir_banco()) as banco:
            linhas = banco.execute(
                f"SELECT {', '.join(_COLUNAS)} FROM sessoes ORDER BY id DESC LIMIT ?", (limite,))
            return [dict(l) for l in linhas]

    def uso(self, *, agora: float) -> dict[str, float]:
        momento = datetime.fromtimestamp(agora)
        dia = momento.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        mes = momento.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        with closing(self._abrir_banco()) as banco:
            def soma(desde: float) -> float:
                (total,) = banco.execute(
                    "SELECT COALESCE(SUM(segundos), 0) FROM sessoes WHERE inicio >= ?", (desde,)).fetchone()
                return float(total)
            (hoje_n,) = banco.execute("SELECT COUNT(*) FROM sessoes WHERE inicio >= ?", (dia,)).fetchone()
            return {"hoje_s": soma(dia), "mes_s": soma(mes), "sessoes_hoje": int(hoje_n)}

    def pode_abrir(self, limites: dict, *, agora: float) -> tuple[bool, str | None]:
        """Os tetos, na ordem em que valem. `(True, None)` ou `(False, motivo)`."""
        with closing(self._abrir_banco()) as banco:
            (abertas,) = banco.execute("SELECT COUNT(*) FROM sessoes WHERE fim IS NULL").fetchone()
            if abertas:
                return False, "busy"
            ultimas = banco.execute(
                "SELECT inicio, fim, motivo FROM sessoes ORDER BY id DESC LIMIT ?",
                (VAZIAS_SEGUIDAS,)).fetchall()
            if (len(ultimas) == VAZIAS_SEGUIDAS
                    and all(l["motivo"] in VAZIAS for l in ultimas)
                    and ultimas[0]["inicio"] - ultimas[-1]["inicio"] <= VAZIAS_JANELA_S
                    and agora - (ultimas[0]["fim"] or agora) < SUSPENSAO_S):
                # Chamado falso em série (televisão, conversa da sala): para de atender um tempo.
                return False, "suspended"
            (na_hora,) = banco.execute(
                "SELECT COUNT(*) FROM sessoes WHERE inicio > ?", (agora - 3600.0,)).fetchone()
            if na_hora >= limites["opens_per_hour"]:
                return False, "too_many_opens"
        uso = self.uso(agora=agora)
        if uso["hoje_s"] >= limites["daily_s"]:
            return False, "daily_limit"
        if uso["mes_s"] >= limites["monthly_s"]:
            return False, "monthly_limit"
        return True, None
