"""Verificações de rede: HTTP(S), certificado SSL/TLS e porta TCP.

Cada função recebe um alvo (dict vindo do targets.yaml) e devolve um
CheckResult. Elas NUNCA lançam exceção para fora: qualquer falha de rede
(timeout, conexão recusada, erro SSL, HTTP 4xx/5xx) vira status DOWN com
uma descrição legível. Assim um alvo com problema não derruba o monitor.
"""
from __future__ import annotations

import logging
import os
import socket
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

# Os três estados possíveis de um alvo
UP, WARN, DOWN = "UP", "WARN", "DOWN"

USER_AGENT = "service-monitor/1.0 (+python-requests)"


@dataclass
class CheckResult:
    """Resultado de UMA verificação de UM alvo."""
    name: str
    target: str
    status: str = UP
    latency_ms: float | None = None
    http_status: int | None = None
    ssl_days_left: int | None = None
    details: list[str] = field(default_factory=list)
    checked_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    def mark(self, status: str, detail: str) -> "CheckResult":
        """Piora o status (nunca melhora: DOWN > WARN > UP) e registra o motivo."""
        severity = {UP: 0, WARN: 1, DOWN: 2}
        if severity[status] > severity[self.status]:
            self.status = status
        self.details.append(detail)
        return self


def run_check(target: dict) -> CheckResult:
    """Escolhe a verificação certa pelo 'type' do alvo. Nunca lança exceção."""
    try:
        if target["type"] == "tcp":
            return check_tcp(target)
        return check_http(target)
    except Exception as exc:  # proteção final: um bug inesperado não pode parar o ciclo
        log.exception("Erro inesperado verificando %s", target["name"])
        return CheckResult(target["name"], str(target.get("url") or target.get("host"))).mark(
            DOWN, f"Erro inesperado: {exc}"
        )


# --------------------------------------------------------------------- HTTP(S)
def check_http(t: dict) -> CheckResult:
    url = t["url"]
    result = CheckResult(name=t["name"], target=url)
    headers, auth = _build_auth(t)
    headers["User-Agent"] = USER_AGENT

    try:
        start = time.perf_counter()
        resp = requests.get(url, headers=headers, auth=auth, timeout=t["timeout"])
        result.latency_ms = round((time.perf_counter() - start) * 1000, 1)
        result.http_status = resp.status_code
    # A ordem importa: SSLError e ConnectTimeout são subclasses de ConnectionError.
    except requests.exceptions.Timeout:
        return result.mark(DOWN, f"Timeout: sem resposta em {t['timeout']}s")
    except requests.exceptions.SSLError as exc:
        return result.mark(DOWN, f"Erro SSL/TLS: {_friendly_error(exc)}")
    except requests.exceptions.ConnectionError as exc:
        return result.mark(DOWN, f"Falha de conexão: {_friendly_error(exc)}")
    except requests.exceptions.RequestException as exc:
        return result.mark(DOWN, f"Erro na requisição: {_short(exc)}")

    # --- Validação condicional da resposta ---
    if resp.status_code not in t["expected_status"]:
        result.mark(DOWN, f"HTTP {resp.status_code} (esperado {t['expected_status']})")
    elif t.get("keyword") and t["keyword"] not in resp.text:
        result.mark(DOWN, f"Texto esperado não encontrado na página: {t['keyword']!r}")
    elif result.latency_ms > t["latency_warn_ms"]:
        result.mark(WARN, f"Lento: {result.latency_ms:.0f} ms (limite {t['latency_warn_ms']} ms)")

    # --- Validade do certificado (só para HTTPS) ---
    parsed = urlparse(url)
    if parsed.scheme == "https":
        try:
            days = get_ssl_days_left(parsed.hostname, parsed.port or 443, t["timeout"])
            result.ssl_days_left = days
            if days < t["ssl_warn_days"]:
                result.mark(WARN, f"Certificado SSL vence em {days} dia(s)")
        except (ssl.SSLError, OSError) as exc:
            result.mark(WARN, f"Não foi possível ler o certificado: {_short(exc)}")

    return result


def _build_auth(t: dict) -> tuple[dict, tuple | None]:
    """Monta cabeçalhos/credenciais lendo os segredos das variáveis de ambiente.

    O YAML só tem o NOME da variável (ex.: token_env: GITHUB_TOKEN);
    o valor real vem do .env, que nunca vai para o GitHub.
    """
    auth_cfg = t.get("auth")
    if not auth_cfg:
        return {}, None

    if auth_cfg["type"] == "bearer":
        token = os.getenv(auth_cfg["token_env"], "").strip()
        if not token:
            log.warning("%s: variável %s vazia; requisição sem autenticação",
                        t["name"], auth_cfg["token_env"])
            return {}, None
        return {"Authorization": f"Bearer {token}"}, None

    # basic auth: o requests codifica usuario:senha em Base64 no cabeçalho
    user = os.getenv(auth_cfg["user_env"], "")
    password = os.getenv(auth_cfg["password_env"], "")
    if not user:
        log.warning("%s: variável %s vazia; requisição sem autenticação",
                    t["name"], auth_cfg["user_env"])
        return {}, None
    return {}, (user, password)


# --------------------------------------------------------------------- SSL/TLS
def get_ssl_days_left(host: str, port: int = 443, timeout: float = 10) -> int:
    """Abre um handshake TLS e devolve quantos dias faltam para o certificado vencer."""
    context = ssl.create_default_context()  # valida cadeia e hostname
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with context.wrap_socket(sock, server_hostname=host) as tls:
            cert = tls.getpeercert()
    expires_at = ssl.cert_time_to_seconds(cert["notAfter"])  # ex.: 'Jan 30 12:00:00 2027 GMT'
    return int((expires_at - time.time()) // 86400)


# --------------------------------------------------------------------- TCP
def check_tcp(t: dict) -> CheckResult:
    """Testa se uma porta TCP aceita conexão (ex.: SSH 22, DNS 53, banco 5432)."""
    host, port = t["host"], t["port"]
    result = CheckResult(name=t["name"], target=f"{host}:{port}")
    try:
        start = time.perf_counter()
        with socket.create_connection((host, port), timeout=t["timeout"]):
            result.latency_ms = round((time.perf_counter() - start) * 1000, 1)
    except TimeoutError:  # precisa vir antes de OSError (é subclasse)
        return result.mark(DOWN, f"Timeout: porta {port} não respondeu em {t['timeout']}s")
    except OSError as exc:
        return result.mark(DOWN, f"Porta {port} inacessível: {_friendly_error(exc)}")

    if result.latency_ms > t["latency_warn_ms"]:
        result.mark(WARN, f"Lento: {result.latency_ms:.0f} ms (limite {t['latency_warn_ms']} ms)")
    return result


# Padrões encontrados no texto das exceções -> mensagem curta e clara
_KNOWN_ERRORS = [
    ("CERTIFICATE_VERIFY_FAILED", "certificado inválido, expirado ou não confiável"),
    ("NameResolution", "DNS não resolveu o nome do host"),
    ("getaddrinfo", "DNS não resolveu o nome do host"),
    ("Connection refused", "conexão recusada (serviço parado?)"),
    ("10061", "conexão recusada (serviço parado?)"),
    ("Connection reset", "conexão encerrada pelo servidor"),
    ("Network is unreachable", "rede inacessível"),
]


def _friendly_error(exc: Exception) -> str:
    """Traduz as mensagens longas do urllib3 para algo legível no alerta."""
    text = str(exc)
    for pattern, message in _KNOWN_ERRORS:
        if pattern in text:
            return message
    return _short(exc)


def _short(exc: Exception, limit: int = 180) -> str:
    """Mensagens do urllib3 são enormes; corta para caber no alerta."""
    text = str(exc) or exc.__class__.__name__
    return text if len(text) <= limit else text[: limit - 3] + "..."
