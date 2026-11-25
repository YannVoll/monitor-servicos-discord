"""Ponto de entrada do monitor.

Uso:
    python main.py              # roda em loop, verificando a cada CHECK_INTERVAL_SECONDS
    python main.py --once       # roda um único ciclo (ideal para cron / Agendador de Tarefas)
    python main.py --report     # envia ao Discord o relatório de uptime das últimas 24h
    python main.py --dry-run    # não envia nada: imprime o JSON que seria enviado

Fluxo de um ciclo:
    GATILHO (timer)  ->  verificações de rede em paralelo (HTTP/SSL/TCP)
                     ->  grava histórico no SQLite
                     ->  compara com o último estado (alerts.evaluate)
                     ->  monta JSON do Discord e envia (somente se algo mudou)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

from monitor.alerts import evaluate
from monitor.checks import run_check
from monitor.config import ConfigError, load_targets
from monitor.notifier import Notifier, build_alert_payloads, build_report_payloads
from monitor.storage import Storage

log = logging.getLogger("monitor")

WEBHOOK_PREFIXES = ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")


def run_cycle(targets: list[dict], storage: Storage, notifier: Notifier) -> None:
    """Executa UMA rodada de verificações e envia alertas das mudanças de status."""
    # Verifica todos os alvos em paralelo: um site lento não atrasa os outros.
    with ThreadPoolExecutor(max_workers=min(10, len(targets))) as pool:
        results = list(pool.map(run_check, targets))

    events, new_states = [], {}
    for target, result in zip(targets, results):
        storage.save_result(result)
        log.info("%-28s %-4s %8s  %s", result.name, result.status,
                 f"{result.latency_ms:.0f}ms" if result.latency_ms is not None else "-",
                 "; ".join(result.details))

        new_state, event = evaluate(result, storage.get_state(result.name),
                                    target["failures_before_alert"])
        new_states[result.name] = new_state
        if event:
            events.append(event)

    delivered = True
    if events:
        log.info("%d mudança(s) de status; notificando o Discord", len(events))
        delivered = notifier.send_all(build_alert_payloads(events))

    # Só grava o novo estado de quem gerou alerta se o alerta foi entregue.
    # Se o Discord falhou, o estado antigo permanece e o alerta é reenviado no próximo ciclo.
    pending = {ev["result"].name for ev in events} if not delivered else set()
    for name, state in new_states.items():
        if name not in pending:
            storage.set_state(name, state)
    if pending:
        log.warning("Alerta não entregue para %s; será reenviado no próximo ciclo",
                    ", ".join(sorted(pending)))


def setup_logging(level: str) -> None:
    Path("logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(), logging.FileHandler("logs/monitor.log", encoding="utf-8")],
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def main() -> int:
    # Garante que emojis/acentos não quebrem o terminal do Windows
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    load_dotenv()  # carrega o .env para os.environ (segredos ficam fora do código)

    parser = argparse.ArgumentParser(description="Monitor de sites/serviços com alertas no Discord")
    parser.add_argument("--once", action="store_true", help="executa um único ciclo e sai")
    parser.add_argument("--report", action="store_true", help="envia relatório de uptime e sai")
    parser.add_argument("--dry-run", action="store_true", help="imprime o JSON em vez de enviar")
    parser.add_argument("--targets", default=os.getenv("TARGETS_FILE", "targets.yaml"))
    args = parser.parse_args()

    setup_logging(os.getenv("LOG_LEVEL", "INFO"))

    # O requests usa estas variáveis como lista de certificados confiáveis. Se apontarem
    # para um arquivo inexistente, TODO acesso HTTPS falha; então ignoramos e avisamos.
    for var in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        bundle = os.environ.get(var)
        if bundle and not Path(bundle).is_file():
            log.warning("%s aponta para arquivo inexistente (%s); ignorando", var, bundle)
            del os.environ[var]

    webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not args.dry_run and not webhook.startswith(WEBHOOK_PREFIXES):
        log.error("DISCORD_WEBHOOK_URL ausente ou inválida. Copie .env.example para .env "
                  "e preencha, ou use --dry-run para testar sem Discord.")
        return 2

    try:
        targets = load_targets(args.targets)
        interval = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
        report_hours = int(os.getenv("REPORT_HOURS", "24"))
    except ConfigError as exc:
        log.error("Configuração inválida: %s", exc)
        return 2
    except ValueError:
        log.error("CHECK_INTERVAL_SECONDS e REPORT_HOURS devem ser números inteiros")
        return 2

    storage = Storage(os.getenv("DB_PATH", "data/monitor.db"))
    notifier = Notifier(webhook, dry_run=args.dry_run)
    log.info("Monitorando %d alvo(s) de %s", len(targets), args.targets)

    try:
        if args.report:
            summary = storage.uptime_summary([t["name"] for t in targets], report_hours)
            return 0 if notifier.send_all(build_report_payloads(summary, report_hours)) else 1

        if args.once:
            run_cycle(targets, storage, notifier)
            return 0

        log.info("Modo contínuo: um ciclo a cada %ds (Ctrl+C para sair)", interval)
        while True:
            started = time.monotonic()
            try:
                run_cycle(targets, storage, notifier)
            except Exception:
                # Nada pode matar o loop silenciosamente: registra e segue para o próximo ciclo.
                log.exception("Erro inesperado no ciclo; tentando novamente no próximo")
            time.sleep(max(0.0, interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        log.info("Encerrado pelo usuário")
        return 0
    finally:
        storage.close()


if __name__ == "__main__":
    sys.exit(main())
