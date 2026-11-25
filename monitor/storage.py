"""Persistência em SQLite: histórico de verificações e último estado de cada alvo.

Tabelas:
  checks -> uma linha por verificação (histórico, usado no relatório de uptime)
  state  -> uma linha por alvo com o último status conhecido; é o que permite
            alertar só quando o status MUDA, e não a cada ciclo.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .checks import CheckResult, UP

SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    status        TEXT NOT NULL,
    latency_ms    REAL,
    http_status   INTEGER,
    ssl_days_left INTEGER,
    details       TEXT,          -- lista de mensagens serializada em JSON
    checked_at    TEXT NOT NULL  -- ISO 8601 em UTC
);
CREATE INDEX IF NOT EXISTS idx_checks_name_time ON checks(name, checked_at);

CREATE TABLE IF NOT EXISTS state (
    name     TEXT PRIMARY KEY,
    status   TEXT NOT NULL,
    failures INTEGER NOT NULL DEFAULT 0,  -- falhas consecutivas
    since    TEXT NOT NULL                -- desde quando está nesse status
);
"""


class Storage:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row  # linhas acessíveis por nome de coluna
        self.conn.executescript(SCHEMA)

    def save_result(self, r: CheckResult) -> None:
        self.conn.execute(
            "INSERT INTO checks (name, status, latency_ms, http_status, ssl_days_left,"
            " details, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (r.name, r.status, r.latency_ms, r.http_status, r.ssl_days_left,
             json.dumps(r.details, ensure_ascii=False), r.checked_at),
        )
        self.conn.commit()

    def get_state(self, name: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM state WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None

    def set_state(self, name: str, state: dict) -> None:
        # UPSERT: insere ou atualiza se o alvo já existir
        self.conn.execute(
            "INSERT INTO state (name, status, failures, since) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET status = excluded.status,"
            " failures = excluded.failures, since = excluded.since",
            (name, state["status"], state["failures"], state["since"]),
        )
        self.conn.commit()

    def uptime_summary(self, names: list[str], hours: int = 24) -> list[dict]:
        """Agrega o histórico: % de disponibilidade e latência média por alvo."""
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        rows = self.conn.execute(
            "SELECT name,"
            "       COUNT(*)                                  AS total,"
            "       SUM(CASE WHEN status != 'DOWN' THEN 1 ELSE 0 END) AS ok,"
            "       AVG(latency_ms)                           AS avg_latency"
            "  FROM checks WHERE checked_at >= ? GROUP BY name",
            (since,),
        ).fetchall()
        by_name = {r["name"]: dict(r) for r in rows}

        summary = []
        for name in names:  # mantém a ordem do targets.yaml
            row = by_name.get(name, {"total": 0, "ok": 0, "avg_latency": None})
            state = self.get_state(name) or {"status": UP}
            uptime = (row["ok"] / row["total"] * 100) if row["total"] else None
            summary.append({
                "name": name,
                "status": state["status"],
                "checks": row["total"],
                "uptime_pct": uptime,
                "avg_latency_ms": row["avg_latency"],
            })
        return summary

    def close(self) -> None:
        self.conn.close()
