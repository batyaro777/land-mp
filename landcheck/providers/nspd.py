"""Живой провайдер НСПД через неофициальную библиотеку pynspd.

На один участок: поиск в слое ЗУ, вкладка ВРИ, пересечение контура с ЗОУИТ и с
территориальными зонами. Каждый шаг изолирован: ошибка шага пишется в payload["errors"],
а не валит проверку. Официального API у НСПД нет — формат может поменяться, поэтому
есть `landcheck monitor`.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any

from landcheck.providers.base import empty_payload

ZOUIT_LAYER_TITLE = "Иные ЗОУИТ"  # все 5 слоёв ЗОУИТ делят categoryId 36940 → один запрос
TERRITORIAL_ZONES_TITLE = "Территориальные зоны"


class LiveNspdProvider:
    name = "nspd"

    def __init__(
        self,
        *,
        with_zones: bool = True,
        timeout: int = 30,
        retries: int = 3,
        proxy: str | None = None,
        pynspd_cache_dir: str | None = None,
    ):
        self.with_zones = with_zones
        self.timeout = timeout
        self.retries = retries
        self.proxy = proxy
        self.pynspd_cache_dir = pynspd_cache_dir
        self._local = threading.local()

    # --- клиент на поток: httpx-клиент pynspd не делим между потоками -------------
    def _client(self):
        client = getattr(self._local, "client", None)
        if client is None:
            from pynspd import Nspd

            client = Nspd(
                client_timeout=self.timeout,
                client_retries=self.retries,
                client_proxy=self.proxy,
                cache_folder_path=self.pynspd_cache_dir,
                trust_env=self.proxy is None and self.pynspd_cache_dir is None,
            )
            self._local.client = client
        return client

    def close(self) -> None:
        client = getattr(self._local, "client", None)
        if client is not None:
            client.close()
            self._local.client = None

    # --- основной метод ---------------------------------------------------------------
    def fetch_raw(self, cad_num: str) -> dict[str, Any]:
        import pynspd.errors as perr
        from pynspd import NspdFeature

        payload = empty_payload(cad_num, self.name)
        nspd = self._client()

        feat = None
        try:
            feat = nspd.find_landplot(cad_num)
        except perr.AmbiguousSearchError:
            feats = nspd.search_landplots(cad_num) or []
            exact = [f for f in feats if f.properties.options.cad_num == cad_num]
            feat = exact[0] if len(exact) == 1 else None
            if feat is None:
                payload["errors"].append("неоднозначный ответ НСПД: несколько объектов по номеру")
        except perr.BlockedIP as e:
            payload["errors"].append(f"НСПД заблокировал IP (403): {e}")
            return payload
        except perr.PynspdError as e:
            # 597 Forbidden — файрвол НСПД режет адрес целиком (зафиксировано 12.10.2026 на VPS).
            if "597" in str(e):
                payload["errors"].append(
                    "НСПД закрыл доступ с этого адреса (597 Forbidden): нужен другой выход в сеть"
                )
            else:
                payload["errors"].append(f"ошибка НСПД при поиске: {e}")
            return payload
        except Exception as e:  # noqa: BLE001 — сеть/прокси; не валим пакет
            payload["errors"].append(f"сбой запроса к НСПД: {type(e).__name__}: {e}")
            return payload

        payload["fetched_at"] = datetime.now(UTC).isoformat()
        if feat is None:
            payload["found"] = False
            return payload

        payload["found"] = True
        payload["landplot"] = feat.model_dump(by_alias=True, mode="json")

        try:
            payload["permission_types"] = nspd.tab_permission_type(feat)
        except Exception as e:  # noqa: BLE001
            payload["errors"].append(f"вкладка ВРИ недоступна: {type(e).__name__}: {e}")

        if not self.with_zones or feat.properties.options.no_coords:
            return payload
        if feat.geometry is None or feat.geometry.type not in ("Polygon", "MultiPolygon"):
            return payload

        contour = feat.geometry.to_shape()
        zouit_def = NspdFeature.by_title(ZOUIT_LAYER_TITLE)
        tz_def = NspdFeature.by_title(TERRITORIAL_ZONES_TITLE)
        try:
            hits = nspd.search_in_contour(contour, zouit_def) or []
            payload["zouit"] = [h.model_dump(by_alias=True, mode="json") for h in hits]
        except Exception as e:  # noqa: BLE001
            payload["errors"].append(f"ЗОУИТ не проверены: {type(e).__name__}: {e}")
        try:
            hits = nspd.search_in_contour(contour, tz_def) or []
            payload["territorial_zones"] = [h.model_dump(by_alias=True, mode="json") for h in hits]
        except Exception as e:  # noqa: BLE001
            payload["errors"].append(f"терзоны не проверены: {type(e).__name__}: {e}")
        return payload
