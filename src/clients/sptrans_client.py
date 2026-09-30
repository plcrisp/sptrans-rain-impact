import time
from typing import Any, Dict, List, Optional
import requests

from src.core.config import Config
from src.utils.logger import get_logger

logger = get_logger("sptrans_client")


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

    def login(self) -> bool:
        """Autentica na API SPTrans via POST /Login/Autenticar?token=..."""
        if not self.token:
            raise ValueError(
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
                    raise RuntimeError(
                        "Autenticação recusada pela SPTrans (resposta 'false'). Verifique a validade do token."
                    )
                else:
                    logger.warning(
                        f"Falha de autenticação SPTrans HTTP {resp.status_code} na tentativa {attempt}/{self.max_retries}."
                    )
            except requests.RequestException as e:
                logger.warning(
                    f"Erro de conexão na autenticação SPTrans ({e}) na tentativa {attempt}/{self.max_retries}."
                )

            if attempt < self.max_retries:
                sleep_s = self.backoff_base ** (attempt - 1)
                time.sleep(sleep_s)

        self._authenticated = False
        raise RuntimeError(
            "Falha ao autenticar na API SPTrans após múltiplas tentativas com backoff."
        )

    def _get_with_retry(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> requests.Response:
        """Executa requisição GET com retry e suporte à reautenticação automática."""
        if not self._authenticated:
            self.login()

        url = f"{self.base_url}{endpoint}"
        reauth_attempted = False

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)

                # Sessão expirada ou não autorizado
                if resp.status_code == 401 or (resp.status_code == 200 and resp.text.strip() == "null"):
                    if not reauth_attempted:
                        logger.info("Sessão SPTrans expirada ou não autorizada. Reautenticando...")
                        self.login()
                        reauth_attempted = True
                        continue

                resp.raise_for_status()
                return resp
            except requests.RequestException as e:
                logger.warning(
                    f"Erro na requisição {endpoint} ({e}) na tentativa {attempt}/{self.max_retries}."
                )

            if attempt < self.max_retries:
                sleep_s = self.backoff_base ** (attempt - 1)
                time.sleep(sleep_s)

        raise RuntimeError(f"Falha ao consultar {endpoint} após {self.max_retries} tentativas.")

    def search_lines(self, termo: str) -> List[Dict[str, Any]]:
        """Busca linhas na SPTrans por letreiro numérico ou termo textual."""
        resp = self._get_with_retry("/Linha/Buscar", params={"termosBusca": termo})
        data = resp.json()
        if not isinstance(data, list):
            return []
        return data

    def get_positions(self, codigo_linha: int) -> Dict[str, Any]:
        """Consulta as posições em tempo real dos veículos de uma linha."""
        resp = self._get_with_retry("/Posicao/Linha", params={"codigoLinha": codigo_linha})
        data = resp.json()
        if not isinstance(data, dict):
            return {"hr": "", "vs": []}
        return data
