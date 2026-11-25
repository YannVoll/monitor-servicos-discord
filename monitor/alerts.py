"""Regra de decisão: quando um resultado deve virar alerta.

Sem isso, um site fora do ar geraria um alerta a cada minuto (spam).
A regra compara o resultado atual com o último estado salvo no banco e só
gera um "evento" quando o status efetivo MUDA (ex.: UP -> DOWN, DOWN -> UP).

failures_before_alert evita alarme falso por uma oscilação momentânea:
com valor 2, o alvo só é declarado DOWN após 2 falhas seguidas.
É uma função pura (sem rede nem banco), por isso é fácil de testar.
"""
from __future__ import annotations

from .checks import CheckResult, DOWN, UP


def evaluate(result: CheckResult, previous: dict | None, failures_before_alert: int):
    """Devolve (novo_estado, evento_ou_None)."""
    prev_status = previous["status"] if previous else None

    # Conta falhas consecutivas; qualquer resultado que não seja DOWN zera o contador.
    if result.status == DOWN:
        failures = (previous["failures"] if previous else 0) + 1
    else:
        failures = 0

    # Ainda não atingiu o limite de falhas? Mantém o status anterior ("confirmando").
    if result.status == DOWN and failures < failures_before_alert:
        effective = prev_status or UP
    else:
        effective = result.status

    changed = effective != prev_status
    new_state = {
        "status": effective,
        "failures": failures,
        "since": result.checked_at if changed else previous["since"],
    }

    # Primeira execução com o alvo OK não é notícia: não alerta.
    if not changed or (previous is None and effective == UP):
        return new_state, None

    event = {
        "result": result,
        "previous": prev_status,
        "current": effective,
        # Para mensagens de "voltou ao ar": desde quando estava com problema
        "problem_since": previous["since"] if previous and prev_status != UP else None,
    }
    return new_state, event
