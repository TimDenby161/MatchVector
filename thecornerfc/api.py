import logging
import time

import requests

from . import config, usage

log = logging.getLogger(__name__)


class QuotaExhausted(RuntimeError):
    pass


class ApiFootball:
    """Thin API-Football v3 client with per-minute and daily quota handling."""

    def __init__(self, api_key=None, daily_reserve=None, min_interval=0.25):
        config.require_api_access("API-Football request")
        api_key = api_key or config.API_KEY
        if not api_key:
            raise RuntimeError("API_FOOTBALL_KEY is not set (see .env.example)")
        self.session = requests.Session()
        self.session.headers["x-apisports-key"] = api_key
        self.daily_reserve = config.API_DAILY_RESERVE if daily_reserve is None else daily_reserve
        self.min_interval = min_interval
        self.daily_remaining = None
        self.calls_made = 0
        self._last_call = 0.0
        self._warned = set()
        # Verify persistence is available before spending quota.
        with usage.connect():
            pass

    def get(self, endpoint, **params):
        """Return the `response` payload for a single request."""
        return self._request(endpoint, params)["response"]

    def get_all_pages(self, endpoint, **params):
        """Follow API-Football `paging` and return all `response` items."""
        items, page = [], 1
        while True:
            data = self._request(endpoint, {**params, "page": page})
            items.extend(data["response"])
            paging = data.get("paging") or {}
            if page >= paging.get("total", 1):
                return items
            page += 1

    def _request(self, endpoint, params, retries=5):
        if self.daily_remaining is not None and self.daily_remaining <= self.daily_reserve:
            raise QuotaExhausted(
                f"Only {self.daily_remaining} daily requests left (reserve {self.daily_reserve})"
            )

        for attempt in range(1, retries + 1):
            config.require_api_access("API-Football request")
            if self.daily_remaining is not None and self.daily_remaining <= self.daily_reserve:
                raise QuotaExhausted(f"Daily reserve reached: {self.daily_remaining}")
            if config.API_RUN_BUDGET:
                with usage.connect() as conn:
                    used = conn.execute("SELECT count(*) FROM api_calls WHERE run_id=?", (usage.run_id(),)).fetchone()[0]
                if used >= config.API_RUN_BUDGET:
                    raise QuotaExhausted(f"Run budget reached: {used}")
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

            started = time.monotonic()
            self.calls_made += 1
            try:
                resp = self.session.get(f"{config.API_BASE_URL}/{endpoint.lstrip('/')}",
                                        params=params, timeout=30)
            except requests.RequestException as exc:
                usage.record(endpoint, params, None, None, None, time.monotonic() - started, False, "transport")
                log.warning("Request error on %s (attempt %d): %s", endpoint, attempt, type(exc).__name__)
                time.sleep(2 ** attempt)
                continue

            duration = time.monotonic() - started
            try:
                data = resp.json()
            except ValueError:
                data = None
            errors = data.get("errors") if isinstance(data, dict) else None
            payload = data.get("response") if isinstance(data, dict) else None
            records = len(payload) if isinstance(payload, (list, dict)) else None
            raw_quota = resp.headers.get("x-ratelimit-requests-remaining")
            quota = int(raw_quota) if raw_quota and str(raw_quota).isdigit() else None
            success = 200 <= resp.status_code < 300 and isinstance(data, dict) and "response" in data and not errors
            usage.record(endpoint, params, resp.status_code, records, quota, duration, success,
                         None if success else ("api" if errors else "http_or_payload"))
            self._read_rate_headers(resp)

            if resp.status_code == 429 or resp.status_code >= 500:
                log.warning("HTTP %s on %s, backing off", resp.status_code, endpoint)
                time.sleep(max(2 ** attempt, 10))
                continue
            resp.raise_for_status()

            data = resp.json()
            errors = data.get("errors")
            if errors:
                # API-Football reports errors in the body with HTTP 200.
                if isinstance(errors, dict) and "rateLimit" in errors:
                    log.warning("Per-minute rate limit hit, sleeping 60s")
                    time.sleep(60)
                    continue
                if isinstance(errors, dict) and "requests" in errors:
                    raise QuotaExhausted(str(errors["requests"]))
                raise RuntimeError(f"API-Football error on {endpoint} {params}: {errors}")
            return data

        raise RuntimeError(f"Giving up on {endpoint} {params} after {retries} attempts")

    def _read_rate_headers(self, resp):
        daily = resp.headers.get("x-ratelimit-requests-remaining")
        if daily is not None:
            try:
                self.daily_remaining = int(daily)
            except (ValueError, TypeError):
                return
            for threshold in config.API_QUOTA_WARN_THRESHOLDS:
                if self.daily_remaining <= threshold and threshold not in self._warned:
                    log.warning("Daily API quota remaining %d is at/below warning threshold %d", self.daily_remaining, threshold)
                    self._warned.add(threshold)
        minute = resp.headers.get("X-RateLimit-Remaining")
        if minute is not None and int(minute) <= 1:
            log.info("Per-minute quota nearly used, pausing 60s")
            time.sleep(60)
