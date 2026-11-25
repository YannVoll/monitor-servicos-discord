"""Carregamento e validação do arquivo de alvos (YAML).

O arquivo targets.yaml descreve O QUE monitorar. Aqui ele é lido, validado
e cada alvo recebe os valores padrão que não foram informados.
Segredos (tokens, senhas) NUNCA ficam no YAML: o YAML só guarda o NOME da
variável de ambiente que contém o segredo (ex.: token_env: GITHUB_TOKEN).
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import yaml

# Valores usados quando nem o alvo nem a seção "defaults" do YAML informam.
DEFAULTS = {
    "timeout": 10,                # segundos para conectar/receber resposta
    "latency_warn_ms": 2000,      # acima disso o alvo fica em ALERTA (WARN)
    "ssl_warn_days": 15,          # avisa quando o certificado vence em menos dias
    "failures_before_alert": 1,   # falhas seguidas antes de declarar FORA DO AR
    "expected_status": [200],     # códigos HTTP considerados "ok"
}

VALID_TYPES = {"http", "tcp"}
VALID_AUTH = {"bearer", "basic"}


class ConfigError(Exception):
    """Erro de configuração com mensagem amigável para o usuário."""


def load_targets(path: str | Path) -> list[dict]:
    """Lê o YAML e devolve a lista de alvos já validados e com defaults aplicados."""
    path = Path(path)
    try:
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)  # safe_load: não executa tags/código do YAML
    except FileNotFoundError:
        raise ConfigError(f"Arquivo de alvos não encontrado: {path}")
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML inválido em {path}: {exc}")

    if not isinstance(data, dict) or not isinstance(data.get("targets"), list):
        raise ConfigError("O YAML precisa ter uma lista 'targets:'")

    # Defaults do YAML sobrescrevem os defaults do código.
    defaults = {**DEFAULTS, **(data.get("defaults") or {})}

    targets: list[dict] = []
    seen_names: set[str] = set()
    for index, raw in enumerate(data["targets"], start=1):
        if not isinstance(raw, dict):
            raise ConfigError(f"Alvo #{index} deve ser um objeto (chave: valor)")

        target = {**defaults, **raw}  # valores do alvo têm prioridade
        target.setdefault("type", "http")
        _validate(target, index)

        if target.get("enabled", True) is False:
            continue  # alvo desligado no YAML: ignora
        if target["name"] in seen_names:
            raise ConfigError(f"Nome de alvo duplicado: {target['name']!r}")
        seen_names.add(target["name"])
        targets.append(target)

    if not targets:
        raise ConfigError("Nenhum alvo habilitado em targets.yaml")
    return targets


def _validate(t: dict, index: int) -> None:
    """Valida um alvo; lança ConfigError descrevendo exatamente o problema."""
    where = f"Alvo #{index} ({t.get('name', 'sem nome')})"

    if not t.get("name"):
        raise ConfigError(f"Alvo #{index}: campo 'name' é obrigatório")
    if t["type"] not in VALID_TYPES:
        raise ConfigError(f"{where}: type deve ser um de {sorted(VALID_TYPES)}")

    if t["type"] == "http":
        url = t.get("url", "")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ConfigError(f"{where}: 'url' inválida: {url!r}")
        # Aceita "expected_status: 200" ou "expected_status: [200, 301]"
        expected = t["expected_status"]
        t["expected_status"] = expected if isinstance(expected, list) else [expected]
        if not all(isinstance(code, int) for code in t["expected_status"]):
            raise ConfigError(f"{where}: expected_status deve conter números inteiros")
    else:  # tcp
        if not t.get("host"):
            raise ConfigError(f"{where}: alvos TCP precisam de 'host'")
        port = t.get("port")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ConfigError(f"{where}: 'port' deve ser inteiro entre 1 e 65535")

    for key in ("timeout", "latency_warn_ms", "ssl_warn_days", "failures_before_alert"):
        if not isinstance(t[key], (int, float)) or t[key] <= 0:
            raise ConfigError(f"{where}: '{key}' deve ser um número positivo")

    auth = t.get("auth")
    if auth is not None:
        if not isinstance(auth, dict) or auth.get("type") not in VALID_AUTH:
            raise ConfigError(f"{where}: auth.type deve ser um de {sorted(VALID_AUTH)}")
        needed = ["token_env"] if auth["type"] == "bearer" else ["user_env", "password_env"]
        for key in needed:
            if not auth.get(key):
                raise ConfigError(f"{where}: auth do tipo {auth['type']} exige '{key}'")
