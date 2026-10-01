import random
import time
from typing import Any, Dict, List, Optional, Tuple
import requests

from src.core.config import Config
from src.utils.logger import get_logger

logger = get_logger("cemaden_client")


class CemadenClientError(Exception):
    """Exceção base para erros do cliente CEMADEN."""
    pass


class CemadenAuthError(CemadenClientError):
    """Exceção lançada quando a autenticação ou reautenticação no CEMADEN falha."""
    pass


class CemadenRequestError(CemadenClientError):
    """Exceção lançada em erros de requisição HTTP à API do CEMADEN."""
    pass


class CemadenJobRejected(CemadenClientError):
    """Exceção lançada quando um agendamento do CEMADEN é rejeitado pelo servidor."""
    pass


class CemadenJobTimeout(CemadenClientError):
    """Exceção lançada quando o tempo de espera pelo agendamento do CEMADEN excede o limite."""
    pass


class CemadenClient:
    """Cliente HTTP com sessão persistente, backoff exponencial, jitter e renovação de token para a API do CEMADEN."""

    def __init__(
        self,
        email: Optional[str] = None,
        password: Optional[str] = None,
        auth_url: Optional[str] = None,
        schedule_url: Optional[str] = None,
        status_url: Optional[str] = None,
        station_url: Optional[str] = None,
        timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
        backoff_base: Optional[int] = None,
    ):
        self.email = email or Config.CEMADEN_EMAIL
        self.password = password or Config.CEMADEN_PASSWORD
        self.auth_url = auth_url or Config.CEMADEN_AUTH_URL
        self.schedule_url = schedule_url or Config.CEMADEN_SCHEDULE_URL
        self.status_url = status_url or Config.CEMADEN_STATUS_URL
        self.station_url = station_url or Config.CEMADEN_STATION_URL

        self.timeout = timeout if timeout is not None else Config.HTTP_TIMEOUT_SECONDS
        self.max_retries = max_retries if max_retries is not None else Config.HTTP_MAX_RETRIES
        self.backoff_base = backoff_base if backoff_base is not None else Config.HTTP_BACKOFF_BASE_SECONDS

        self.session = requests.Session()
        self.token: Optional[str] = None

    def close(self) -> None:
        """Encerra a sessão HTTP subjacente."""
        if self.session is not None:
            self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def login(self) -> bool:
        """Autentica na API do CEMADEN e guarda o token em memória."""
        if not self.email or not self.password or not self.auth_url:
            raise CemadenAuthError(
                "Credenciais ou URL de autenticação CEMADEN não configuradas no ambiente."
            )

        payload = {"email": self.email, "password": self.password}
        attempts = 0

        for attempt in range(1, self.max_retries + 1):
            attempts += 1
            try:
                resp = self.session.post(self.auth_url, json=payload, timeout=self.timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    token = data.get("access_token") or data.get("token")
                    if token:
                        self.token = str(token).strip()
                        self.session.headers["token"] = self.token
                        logger.info("Autenticação com CEMADEN realizada com sucesso.")
                        return True
                    raise CemadenAuthError("Resposta de autenticação 200 OK sem token presente.")
                elif resp.status_code in (401, 403):
                    raise CemadenAuthError(
                        f"Credenciais inválidas ou acesso não autorizado no CEMADEN (HTTP {resp.status_code})."
                    )
                else:
                    logger.warning(
                        f"Falha de autenticação CEMADEN HTTP {resp.status_code} na tentativa {attempt}/{self.max_retries}."
                    )
            except requests.RequestException as e:
                logger.warning(
                    f"Erro de conexão na autenticação CEMADEN ({type(e).__name__}) na tentativa {attempt}/{self.max_retries}."
                )

            if attempt < self.max_retries:
                jitter = random.uniform(0.1, 0.5)
                sleep_s = (self.backoff_base ** (attempt - 1)) + jitter
                time.sleep(sleep_s)

        raise CemadenAuthError("Falha ao autenticar no CEMADEN após múltiplas tentativas com backoff.")

    def _request_with_retry(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        stream: bool = False,
    ) -> Tuple[requests.Response, float, int]:
        """Executa chamada HTTP com retry exponencial, jitter e renovação de token em caso de HTTP 401."""
        if not self.token:
            self.login()

        reauth_done = False
        attempts = 0

        for attempt in range(1, self.max_retries + 1):
            attempts += 1
            t0 = time.perf_counter()
            try:
                resp = self.session.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_data,
                    timeout=self.timeout,
                    stream=stream,
                )
                latency_s = time.perf_counter() - t0

                # 1. Trata 401 (token expirado ou taxa limite por token)
                if resp.status_code == 401:
                    resp_text = resp.text
                    if "excedeu o número máximo" in resp_text.lower() or "alerta" in resp_text.lower():
                        logger.warning(
                            f"Taxa limite temporária do CEMADEN atingida (tentativa {attempt}/{self.max_retries}). "
                            "Aguardando 15s para liberação da janela de acesso..."
                        )
                        time.sleep(15.0)
                        continue

                    if not reauth_done:
                        logger.info("Token CEMADEN expirado ou inválido (HTTP 401). Renovando autenticação...")
                        self.login()
                        reauth_done = True
                        continue
                    else:
                        raise CemadenAuthError(f"Falha persistente de autorização CEMADEN (HTTP 401): {resp_text[:100]}")

                # 2. Erros 4xx (exceto 401) -> não faz retry
                if 400 <= resp.status_code < 500:
                    raise CemadenRequestError(
                        f"Erro de cliente HTTP {resp.status_code} em chamada CEMADEN."
                    )

                # 3. Erros 5xx -> retry com backoff
                if resp.status_code >= 500:
                    logger.warning(
                        f"Servidor CEMADEN retornou HTTP {resp.status_code} (tentativa {attempt}/{self.max_retries})."
                    )
                else:
                    return resp, latency_s, attempts

            except (requests.Timeout, requests.ConnectionError) as e:
                logger.warning(
                    f"Erro de rede em chamada CEMADEN ({type(e).__name__}) na tentativa {attempt}/{self.max_retries}."
                )
            except (CemadenAuthError, CemadenRequestError):
                raise
            except Exception as e:
                logger.warning(
                    f"Exceção inesperada em chamada CEMADEN ({type(e).__name__}) na tentativa {attempt}/{self.max_retries}."
                )

            if attempt < self.max_retries:
                jitter = random.uniform(0.1, 0.5)
                sleep_s = (self.backoff_base ** (attempt - 1)) + jitter
                time.sleep(sleep_s)

        raise CemadenRequestError(f"Falha na requisição CEMADEN após {self.max_retries} tentativas.")

    def schedule(
        self,
        station_id: str,
        uf: str,
        start_dt: str,
        end_dt: str,
        network_id: Optional[str] = None,
        sensor_id: Optional[str] = None,
    ) -> Tuple[int, float, int]:
        """Agenda um job de extração de dados brutos na API do CEMADEN."""
        if not self.schedule_url:
            raise CemadenRequestError("CEMADEN_SCHEDULE_URL não configurada.")

        params = {
            "arquivo": "CSV",
            "codestacao": station_id,
            "datainicio": start_dt,
            "datafim": end_dt,
            "rede": network_id or Config.CEMADEN_NETWORK_ID,
            "sensor": sensor_id or Config.CEMADEN_SENSOR_ID,
            "uf": uf,
        }

        resp, latency_s, attempts = self._request_with_retry("GET", self.schedule_url, params=params)
        data = resp.json()

        # O retorno pode ser um dicionário {"id": 123} ou lista [{"id": 123}]
        job_id = None
        if isinstance(data, dict):
            job_id = data.get("id")
        elif isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
            job_id = data[0].get("id")

        if job_id is None:
            raise CemadenRequestError(f"Resposta de agendamento não contém job_id válido: {data}")

        return int(job_id), latency_s, attempts

    def list_jobs(self) -> Tuple[List[Dict[str, Any]], float, int]:
        """Consulta a lista completa de agendamentos da conta no CEMADEN."""
        if not self.status_url:
            raise CemadenRequestError("CEMADEN_STATUS_URL não configurada.")

        resp, latency_s, attempts = self._request_with_retry("GET", self.status_url)
        data = resp.json()

        if not isinstance(data, list):
            logger.warning(f"Retorno inesperado em status_url: esperado lista, recebido {type(data).__name__}.")
            data = []

        return data, latency_s, attempts

    def download(self, link: str) -> Tuple[bytes, float, int]:
        """Baixa os bytes brutos do arquivo ZIP gerado pelo CEMADEN a partir do link retornado."""
        resp, latency_s, attempts = self._request_with_retry("GET", link)
        return resp.content, latency_s, attempts

    def get_station_info(self, station_id: str) -> Tuple[List[Dict[str, Any]], float, int]:
        """Consulta metadados e status em tempo real da estação informada."""
        if not self.station_url:
            raise CemadenRequestError("CEMADEN_STATION_URL não configurada.")

        params = {"codestacao": station_id, "formato": "JSON"}
        resp, latency_s, attempts = self._request_with_retry("GET", self.station_url, params=params)
        data = resp.json()

        if isinstance(data, dict):
            data = [data]
        elif not isinstance(data, list):
            data = []

        return data, latency_s, attempts
