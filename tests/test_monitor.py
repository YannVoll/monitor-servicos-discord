"""Testes da lógica que não depende de rede: regra de alerta, config e payload.

Rodar com:  python -m pytest -q
"""
import pytest

from monitor.alerts import evaluate
from monitor.checks import DOWN, UP, WARN, CheckResult
from monitor.config import ConfigError, load_targets
from monitor.notifier import MAX_EMBEDS, build_alert_payloads


def result(status, when="2026-11-25T12:00:00+00:00"):
    return CheckResult(name="site", target="https://x", status=status, checked_at=when)


# ---------------------------------------------------------------- evaluate
def test_primeira_execucao_ok_nao_alerta():
    state, event = evaluate(result(UP), None, 1)
    assert state["status"] == UP and event is None


def test_queda_gera_alerta():
    prev = {"status": UP, "failures": 0, "since": "2026-11-25T11:00:00+00:00"}
    state, event = evaluate(result(DOWN), prev, 1)
    assert state["status"] == DOWN
    assert event["previous"] == UP and event["current"] == DOWN


def test_queda_so_alerta_apos_limite_de_falhas():
    prev = {"status": UP, "failures": 0, "since": "x"}
    state, event = evaluate(result(DOWN), prev, 2)
    assert event is None and state["status"] == UP and state["failures"] == 1

    state, event = evaluate(result(DOWN), state, 2)
    assert event is not None and state["status"] == DOWN


def test_status_igual_nao_repete_alerta():
    prev = {"status": DOWN, "failures": 3, "since": "x"}
    state, event = evaluate(result(DOWN), prev, 1)
    assert event is None and state["since"] == "x"


def test_recuperacao_informa_desde_quando():
    prev = {"status": DOWN, "failures": 2, "since": "2026-11-25T11:00:00+00:00"}
    state, event = evaluate(result(UP), prev, 1)
    assert event["current"] == UP
    assert event["problem_since"] == "2026-11-25T11:00:00+00:00"
    assert state["failures"] == 0


# ---------------------------------------------------------------- config
def write(tmp_path, text):
    path = tmp_path / "targets.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_config_aplica_defaults(tmp_path):
    targets = load_targets(write(tmp_path, "targets:\n  - name: a\n    url: https://a.com\n"))
    assert targets[0]["timeout"] == 10 and targets[0]["expected_status"] == [200]


def test_config_ignora_desabilitado(tmp_path):
    text = ("targets:\n  - name: a\n    url: https://a.com\n"
            "  - name: b\n    url: https://b.com\n    enabled: false\n")
    assert [t["name"] for t in load_targets(write(tmp_path, text))] == ["a"]


@pytest.mark.parametrize("text", [
    "targets:\n  - name: a\n    url: ftp://a.com\n",
    "targets:\n  - name: a\n    type: tcp\n    host: x\n    port: 99999\n",
    "targets:\n  - name: a\n    url: https://a\n  - name: a\n    url: https://b\n",
    "targets:\n  - name: a\n    url: https://a\n    auth: {type: bearer}\n",
    "nada: 1\n",
])
def test_config_invalida(tmp_path, text):
    with pytest.raises(ConfigError):
        load_targets(write(tmp_path, text))


# ---------------------------------------------------------------- payload
def test_payload_agrupa_em_lotes_de_10():
    events = [{"result": result(DOWN), "previous": UP, "current": DOWN, "problem_since": None}
              for _ in range(12)]
    payloads = build_alert_payloads(events)
    assert len(payloads) == 2
    assert len(payloads[0]["embeds"]) == MAX_EMBEDS and len(payloads[1]["embeds"]) == 2
    assert payloads[0]["embeds"][0]["color"] == 0xE74C3C


def test_payload_warn_tem_cor_amarela():
    r = result(WARN)
    r.details.append("Lento")
    payload = build_alert_payloads([{"result": r, "previous": UP, "current": WARN,
                                     "problem_since": None}])[0]
    assert payload["embeds"][0]["color"] == 0xF1C40F
    assert "Lento" in payload["embeds"][0]["description"]
