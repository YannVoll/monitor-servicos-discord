"""Envio de notificações para o Discord via Webhook (HTTP POST com JSON).

1. build_*_payloads() transformam os resultados em JSON no formato "embed" do
   Discord (título, cor, campos). Discord aceita no máximo 10 embeds por mensagem,
   então os eventos são agrupados em lotes de 10.
2. Notifier.send() faz o POST com tratamento de erros:
     - 204/200          -> sucesso
     - 429 rate limit   -> espera o tempo pedido pelo Discord (retry_after) e tenta de novo
     - 5xx / rede       -> tenta de novo com espera exponencial (2s, 4s, 8s)
     - outros 4xx       -> erro de configuração (webhook inválido); não adianta repetir
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

import requests

from .checks import DOWN, UP, WARN

log = logging.getLogger(__name__)

COLORS = {UP: 0x2ECC71, WARN: 0xF1C40F, DOWN: 0xE74C3C}  # verde, amarelo, vermelho
EMOJI = {UP: "🟢", WARN: "🟡", DOWN: "🔴"}
LABEL = {UP: "ONLINE", WARN: "EM ALERTA", DOWN: "FORA DO AR"}
BOT_NAME = "Monitor de Serviços"
MAX_EMBEDS = 10


# ------------------------------------------------------------------ payloads
def build_alert_payloads(events: list[dict]) -> list[dict]:
    """Converte eventos de mudança de status em uma ou mais mensagens do Discord."""
    embeds = [_event_embed(ev) for ev in events]
    payloads = []
    for i in range(0, len(embeds), MAX_EMBEDS):
        batch = embeds[i:i + MAX_EMBEDS]
        down = sum(1 for ev in events[i:i + MAX_EMBEDS] if ev["current"] == DOWN)
        header = f"**{len(batch)} mudança(s) de status detectada(s)**"
        if down:
            header += f" — {down} serviço(s) fora do ar"
        payloads.append({"username": BOT_NAME, "content": header, "embeds": batch})
    return payloads


def _event_embed(ev: dict) -> dict:
    r, current = ev["result"], ev["current"]

    if current == UP and ev["previous"] in (DOWN, WARN):
        title = f"{EMOJI[UP]} {r.name} voltou ao normal"
        description = "O serviço voltou a responder corretamente."
        if ev["problem_since"]:
            description += f"\nDuração do problema: **{_duration_since(ev['problem_since'])}**"
    else:
        title = f"{EMOJI[current]} {r.name} está {LABEL[current]}"
        description = "\n".join(f"• {d}" for d in r.details) or "Sem detalhes."

    fields = [{"name": "Alvo", "value": r.target[:1024], "inline": False}]
    if r.http_status is not None:
        fields.append({"name": "HTTP", "value": str(r.http_status), "inline": True})
    if r.latency_ms is not None:
        fields.append({"name": "Latência", "value": f"{r.latency_ms:.0f} ms", "inline": True})
    if r.ssl_days_left is not None:
        fields.append({"name": "Certificado SSL", "value": f"vence em {r.ssl_days_left} dias",
                       "inline": True})

    return {
        "title": title[:256],
        "description": description[:4000],
        "color": COLORS[current],
        "fields": fields,
        "timestamp": r.checked_at,
        "footer": {"text": f"Status anterior: {LABEL[ev['previous']]}" if ev["previous"]
                   else "Primeira verificação deste alvo"},
    }


def build_report_payloads(summary: list[dict], hours: int) -> list[dict]:
    """Relatório agregado de disponibilidade (uptime) de todos os alvos."""
    lines = []
    for s in summary:
        uptime = f"{s['uptime_pct']:.1f}%" if s["uptime_pct"] is not None else "sem dados"
        latency = f"{s['avg_latency_ms']:.0f} ms" if s["avg_latency_ms"] is not None else "-"
        lines.append(f"{EMOJI[s['status']]} **{s['name']}** — uptime {uptime} · "
                     f"latência média {latency} · {s['checks']} verificações")

    down = sum(1 for s in summary if s["status"] == DOWN)
    color = COLORS[DOWN] if down else COLORS[UP]
    embed = {
        "title": f"📊 Relatório de disponibilidade — últimas {hours}h",
        "description": "\n".join(lines)[:4000] or "Nenhum alvo configurado.",
        "color": color,
        "footer": {"text": f"{len(summary)} alvos · {down} fora do ar agora"},
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return [{"username": BOT_NAME, "embeds": [embed]}]


def _duration_since(iso_timestamp: str) -> str:
    seconds = int((datetime.now(timezone.utc) - datetime.fromisoformat(iso_timestamp)).total_seconds())
    hours, rest = divmod(max(seconds, 0), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}min"
    return f"{minutes}min {secs}s" if minutes else f"{secs}s"


# ------------------------------------------------------------------ envio
class Notifier:
    def __init__(self, webhook_url: str, dry_run: bool = False, max_retries: int = 3,
                 timeout: float = 10):
        self.webhook_url = webhook_url
        self.dry_run = dry_run
        self.max_retries = max_retries
        self.timeout = timeout

    def send_all(self, payloads: list[dict]) -> bool:
        """Envia todas as mensagens; True somente se todas foram entregues."""
        ok = True
        for payload in payloads:
            ok = self.send(payload) and ok
        return ok

    def send(self, payload: dict) -> bool:
        if self.dry_run:
            # Modo de teste: mostra o JSON que seria enviado, sem usar a rede.
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return True

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.post(self.webhook_url, json=payload, timeout=self.timeout)
            except (requests.exceptions.RequestException, OSError) as exc:
                # OSError cobre falhas locais (ex.: arquivo de certificados inválido).
                # Obs.: nunca registramos a URL do webhook no log, ela contém o token.
                log.warning("Discord inacessível (tentativa %d/%d): %s",
                            attempt, self.max_retries, exc.__class__.__name__)
                self._backoff(attempt)
                continue

            if resp.status_code in (200, 204):
                return True

            if resp.status_code == 429:  # rate limit: o Discord diz quanto esperar
                wait = _retry_after(resp)
                log.warning("Discord rate limit; aguardando %.1fs", wait)
                time.sleep(wait)
                continue

            if resp.status_code >= 500:
                log.warning("Discord com erro HTTP %d (tentativa %d/%d)",
                            resp.status_code, attempt, self.max_retries)
                self._backoff(attempt)
                continue

            # 400, 401, 404...: problema no webhook ou no payload. Repetir não resolve.
            log.error("Discord recusou a mensagem (HTTP %d): %s",
                      resp.status_code, resp.text[:300])
            return False

        log.error("Falha ao notificar o Discord após %d tentativas", self.max_retries)
        return False

    def _backoff(self, attempt: int) -> None:
        if attempt < self.max_retries:
            time.sleep(2 ** attempt)  # 2s, 4s, 8s...


def _retry_after(resp: requests.Response) -> float:
    try:
        return min(float(resp.json().get("retry_after", 1)), 60)
    except (ValueError, AttributeError):
        return 1.0
