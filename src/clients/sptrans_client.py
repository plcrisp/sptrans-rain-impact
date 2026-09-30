from dataclasses import dataclass
import random
import time
from typing import Any, Dict, List, Optional
import requests

from src.core.config import Config
from src.utils.logger import get_logger

logger = get_logger("sptrans_client")


class SPTransError(Exception):
    """Exceção base para erros do cliente SPTrans."""
    pass


class SPTransAuthError(SPTransError):
    """Exceção lançada quando a autenticação ou reautenticação na SPTrans falha."""
    pass


class SPTransRequestError(SPTransError):
    """Exceção lançada em erros de requisição à API SPTrans."""
    pass


@dataclass
class PositionResponse:
    """Objeto de resposta detalhado para a consulta de posições de veículos."""
    payload: Dict[str, Any]
    http_status: int
    latency_ms: float
    attempts: int
    relogin: bool

    # Suporte a interface de dicionário para manter compatibilidade total com a Task 1
    def __getitem__(self, key: str) -> Any:
        return self.payload[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in self.payload

    def keys(self):
        return self.payload.keys()

    def values(self):
        return self.payload.values()

    def items(self):
        return self.payload.items()


class SPTransClient:
    """Cliente HTTP para autenticação e consultas à API Olho Vivo da SPTrans."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
        backoff_base: Optional[int] = None,
    ):
        self.base_url = (base_url or Config.SPTRANS_BASE_URL).rstrip("/")
        self.token = token or Config.SPTRANS_TOKEN
        self.timeout = timeout if timeout is not None else Config.HTTP_TIMEOUT_SECONDS
        self.max_retries = (
            max_retries if max_retries is not None else Config.HTTP_MAX_RETRIES
        )
        self.backoff_base = (
            backoff_base if backoff_base is not None else Config.HTTP_BACKOFF_BASE_SECONDS
        )
        self.session = requests.Session()
        self._authenticated = False

    def close(self) -> None:
        """Fecha a sessão HTTP subjacente."""
        if self.session is not None:
            self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def login(self) -> bool:
        """Autentica na API SPTrans via POST /Login/Autenticar?token=...

        Nunca loga o token nem a URL de autenticação contendo parâmetros sensíveis.
        """
        if not self.token:
            raise SPTransAuthError(
                "SPTRANS_TOKEN não configurado no ambiente/.env para login na SPTrans."
            )

        url = f"{self.base_url}/Login/Autenticar"
        params = {"token": self.token}

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.post(url, params=params, timeout=self.timeout)
                if resp.status_code == 200 and resp.text.strip().lower() == "true":
                    self._authenticated = True
                    logger.info("Autenticação com SPTrans Olho Vivo realizada com sucesso.")
                    return True
                elif resp.status_code == 200 and resp.text.strip().lower() == "false":
                    raise SPTransAuthError(
                        "Autenticação recusada pela SPTrans (resposta 'false'). Verifique a validade do token."
                    )
                else:
                    logger.warning(
                        f"Falha de autenticação SPTrans HTTP {resp.status_code} na tentativa {attempt}/{self.max_retries}."
                    )
            except requests.RequestException as e:
                logger.warning(
                    f"Erro de conexão na autenticação SPTrans na tentativa {attempt}/{self.max_retries}: {type(e).__name__}"
                )

            if attempt < self.max_retries:
                jitter = random.uniform(0.1, 0.5)
                sleep_s = (self.backoff_base ** (attempt - 1)) + jitter
                time.sleep(sleep_s)

        self._authenticated = False
        raise SPTransAuthError(
            "Falha ao autenticar na API SPTrans após múltiplas tentativas com backoff."
        )

    def _is_session_invalid(self, resp: requests.Response) -> bool:
        """Verifica se o status ou o corpo HTTP indicam sessão expirada ou não autorizada."""
        if resp.status_code in (401, 403):
            return True
        text = resp.text.strip()
        if resp.status_code == 200 and (text == "null" or "Authorization has been denied" in text):
            return True
        return False

    def _get_with_retry(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> tuple[requests.Response, float, int, bool]:
        """Executa requisição GET com timeout, retry exponencial + jitter e reautenticação automática.

        Retorna tupla: (Response, latency_ms, attempts_used, relogin_occurred).
        """
        if not self._authenticated:
            self.login()

        url = f"{self.base_url}{endpoint}"
        reauth_done = False
        attempts = 0

        for attempt in range(1, self.max_retries + 1):
            attempts += 1
            t0 = time.perf_counter()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                latency_ms = (time.perf_counter() - t0) * 1000

                # 1. Detecção de sessão expirada / inválida
                if self._is_session_invalid(resp):
                    if not reauth_done:
                        logger.info("Sessão SPTrans expirada ou não autorizada (401). Reautenticando...")
                        self.login()
                        reauth_done = True
                        # Repete imediatamente após relogin
                        continue
                    else:
                        raise SPTransAuthError(
                            f"Falha de autenticação SPTrans persistente em {endpoint} (HTTP {resp.status_code})."
                        )

                # 2. Erros 4xx (exceto autenticação) -> Não faz retry
                if 400 <= resp.status_code < 500:
                    resp.raise_for_status()

                # 3. Erros 5xx -> Retry com backoff
                if resp.status_code >= 500:
                    logger.warning(
                        f"Servidor SPTrans retornou HTTP {resp.status_code} em {endpoint} (tentativa {attempt}/{self.max_retries})."
                    )
                else:
                    return resp, latency_ms, attempts, reauth_done

            except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as e:
                # Se for 4xx (que não 401), não tentar novamente
                if isinstance(e, requests.HTTPError) and e.response is not None and 400 <= e.response.status_code < 500:
                    raise SPTransRequestError(f"Erro cliente HTTP {e.response.status_code} em {endpoint}.") from e

                logger.warning(
                    f"Erro de rede/HTTP em {endpoint} ({type(e).__name__}) na tentativa {attempt}/{self.max_retries}."
                )
            except SPTransAuthError:
                raise
            except Exception as e:
                logger.warning(
                    f"Exceção inesperada em {endpoint} ({type(e).__name__}) na tentativa {attempt}/{self.max_retries}."
                )

            if attempt < self.max_retries:
                jitter = random.uniform(0.1, 0.5)
                sleep_s = (self.backoff_base ** (attempt - 1)) + jitter
                time.sleep(sleep_s)

        raise SPTransRequestError(
            f"Falha ao consultar {endpoint} após {self.max_retries} tentativas."
        )

    def search_lines(self, termo: str) -> List[Dict[str, Any]]:
        """Busca linhas na SPTrans por letreiro numérico ou termo textual."""
        resp, _, _, _ = self._get_with_retry("/Linha/Buscar", params={"termosBusca": termo})
        data = resp.json()
        if not isinstance(data, list):
            return []
        return data

    def get_positions(self, codigo_linha: int) -> PositionResponse:
        """Consulta as posições em tempo real dos veículos de uma linha.

        Retorna PositionResponse contendo:
        - payload: JSON exatamente como veio da API
        - http_status: status HTTP da resposta
        - latency_ms: tempo de resposta da chamada em milissegundos
        - attempts: número de tentativas necessárias
        - relogin: bool indicando se precisou reautenticar
        """
        return self.get_positions_detailed(codigo_linha)

    def get_positions_detailed(self, codigo_linha: int) -> PositionResponse:
        """Consulta as posições em tempo real dos veículos de uma linha com metadados."""
        resp, latency_ms, attempts, relogin = self._get_with_retry(
            "/Posicao/Linha", params={"codigoLinha": codigo_linha}
        )
        try:
            payload = resp.json()
        except Exception:
            payload = {"hr": "", "vs": []}

        if not isinstance(payload, dict):
            payload = {"hr": "", "vs": []}

        return PositionResponse(
            payload=payload,
            http_status=resp.status_code,
            latency_ms=round(latency_ms, 2),
            attempts=attempts,
            relogin=relogin,
        )
