from __future__ import annotations

from typing import Any, Callable, Optional

from .sub2api_auth import Sub2ApiAuthService
from .sub2api_captcha import CamoufoxCaptchaSolver
from .sub2api_keys import Sub2ApiKeyService
from .sub2api_transport import Sub2ApiClient


class Sub2ApiAccountSession:
    """One authenticated account operation boundary."""

    def __init__(self, origin: str, email: str, password: str, *, proxies: Optional[dict[str, str]] = None, log_callback: Optional[Callable[[str], None]] = None) -> None:
        self.solver = CamoufoxCaptchaSolver(log_callback=log_callback)
        self.client = Sub2ApiClient(origin, timeout=30, proxies=proxies)
        try:
            auth = Sub2ApiAuthService(self.client, self.solver)
            self.token = auth.login(email, password, auth.public_settings())
            self.keys = Sub2ApiKeyService(self.client)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        try:
            self.solver.close()
        finally:
            self.client.close()

    def __enter__(self) -> "Sub2ApiAccountSession": return self
    def __exit__(self, *_: Any) -> None: self.close()
