"""合规、保守的公开产品页元数据读取。

这里只读取用户明确粘贴的单个 URL，不提供站内搜索、批量遍历、登录或封禁规避。
限速、暂停和缓存均写入 SQLite，避免通过重启应用意外绕过站点保护。
"""

from __future__ import annotations

import ipaddress
import json
import socket
import urllib.robotparser
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from .database import Database, DomainError, utc_now

MIN_INTERVAL_SECONDS = 30
DAILY_LIMIT = 20
CACHE_DAYS = 30
BLOCK_HOURS = 24
MAX_BYTES = 2 * 1024 * 1024
USER_AGENT = "component-library-local/0.2 (user-initiated metadata preview)"
PROHIBITED_HOSTS = ("lcsc.com", "szlcsc.com", "jlc.com")


def _parse_time(value: str | None) -> datetime | None:
    """解析数据库中的 UTC 时间。"""

    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


class PublicWebEnricher:
    """带持久化访问策略的产品页读取器。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def preview(self, raw_url: str) -> dict[str, Any]:
        """读取一个公开产品页并提取 JSON-LD/HTML 元数据。"""

        url = self._validate_url(raw_url)
        host = urlparse(url).hostname or ""
        cached = self._cached(url)
        if cached is not None:
            cached["from_cache"] = True
            return cached
        self._validate_dns(host)
        self._reserve(host)

        import httpx

        with httpx.Client(
            headers={"User-Agent": USER_AGENT}, follow_redirects=False, timeout=12, trust_env=False
        ) as client:
            self._check_robots(client, url)
            response = client.get(url)
        if response.status_code in (403, 429):
            self._block(host)
            raise DomainError("WEB_PAUSED", f"站点返回 {response.status_code}，已暂停该站 24 小时")
        if 300 <= response.status_code < 400:
            raise DomainError("WEB_REDIRECT", "页面发生重定向，请粘贴最终公开 HTTPS URL")
        if response.status_code < 200 or response.status_code >= 300:
            raise DomainError("WEB_ERROR", f"页面返回 HTTP {response.status_code}，不会自动重试")
        if len(response.content) > MAX_BYTES:
            raise DomainError("WEB_TOO_LARGE", "页面超过 2 MB 安全上限")
        result = self._parse(url, response.text)
        self._save_cache(url, result)
        return result

    @staticmethod
    def _validate_url(raw_url: str) -> str:
        """限制为不含凭据和自定义端口的 HTTPS URL。"""

        parsed = urlparse(raw_url.strip())
        if parsed.scheme != "https" or not parsed.hostname:
            raise DomainError("VALIDATION_ERROR", "只允许完整的 HTTPS 公开页面")
        if parsed.username or parsed.password or parsed.port:
            raise DomainError("VALIDATION_ERROR", "URL 不得包含凭据或非标准端口")
        host = parsed.hostname.casefold()
        if any(host == blocked or host.endswith("." + blocked) for blocked in PROHIBITED_HOSTS):
            raise DomainError("WEB_PROHIBITED", "该站点禁止未授权自动访问，请在浏览器中手工查看")
        return raw_url.strip()

    @staticmethod
    def _validate_dns(host: str) -> None:
        """拒绝解析到本机、私有、链路或保留地址的域名。"""

        try:
            addresses = {
                item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            }
        except socket.gaierror as error:
            raise DomainError("WEB_DNS", "无法解析网站域名") from error
        if not addresses:
            raise DomainError("WEB_DNS", "网站域名没有可用地址")
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global:
                raise DomainError("WEB_PRIVATE_ADDRESS", "不允许访问本机、私有或保留网络地址")

    def _reserve(self, host: str) -> None:
        """原子检查并增加域名访问次数。"""

        now = datetime.now(UTC)
        day_key = now.date().isoformat()
        with self.database.write() as connection:
            row = connection.execute(
                "SELECT * FROM web_host_state WHERE host=?", (host,)
            ).fetchone()
            if row:
                blocked = _parse_time(row["blocked_until"])
                if blocked and blocked > now:
                    raise DomainError("WEB_PAUSED", "该站点仍处于 24 小时暂停期")
                count = int(row["day_count"]) if row["day_key"] == day_key else 0
                last = _parse_time(row["last_request_at"])
                if count >= DAILY_LIMIT:
                    raise DomainError("WEB_DAILY_LIMIT", "该站点已达到每日 20 次上限")
                if last and (now - last).total_seconds() < MIN_INTERVAL_SECONDS:
                    wait = MIN_INTERVAL_SECONDS - int((now - last).total_seconds())
                    raise DomainError("WEB_RATE_LIMIT", f"访问过于频繁，请在 {wait} 秒后重试")
                connection.execute(
                    "UPDATE web_host_state SET last_request_at=?,day_key=?,day_count=? WHERE host=?",
                    (utc_now(), day_key, count + 1, host),
                )
            else:
                connection.execute(
                    "INSERT INTO web_host_state(host,last_request_at,day_key,day_count) VALUES(?,?,?,1)",
                    (host, utc_now(), day_key),
                )

    def _block(self, host: str) -> None:
        """持久化 403/429 暂停时间。"""

        until = (
            (datetime.now(UTC) + timedelta(hours=BLOCK_HOURS)).isoformat().replace("+00:00", "Z")
        )
        with self.database.write() as connection:
            connection.execute(
                "UPDATE web_host_state SET blocked_until=? WHERE host=?", (until, host)
            )

    def _check_robots(self, client: Any, url: str) -> None:
        """检查 robots；无法确认时按保守策略停止。"""

        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        response = client.get(robots_url)
        if response.status_code == 404:
            return
        if response.status_code != 200:
            raise DomainError("WEB_ROBOTS", f"robots.txt 返回 {response.status_code}，已停止")
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(response.text.splitlines())
        if not parser.can_fetch(USER_AGENT, url):
            raise DomainError("WEB_ROBOTS", "robots.txt 不允许读取该页面")

    def _cached(self, url: str) -> dict[str, Any] | None:
        """返回尚未过期的页面缓存。"""

        now = datetime.now(UTC)
        with self.database.read() as connection:
            row = connection.execute("SELECT * FROM web_page_cache WHERE url=?", (url,)).fetchone()
        if row and (_parse_time(row["expires_at"]) or now) > now:
            cached: dict[str, Any] = json.loads(row["metadata_json"])
            return cached
        return None

    def _save_cache(self, url: str, value: dict[str, Any]) -> None:
        """保存页面元数据，原始 HTML 不落盘。"""

        expires = (
            (datetime.now(UTC) + timedelta(days=CACHE_DAYS)).isoformat().replace("+00:00", "Z")
        )
        with self.database.write() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO web_page_cache(url,metadata_json,expires_at) VALUES(?,?,?)",
                (url, json.dumps(value, ensure_ascii=False), expires),
            )

    @staticmethod
    def _parse(url: str, html: str) -> dict[str, Any]:
        """优先解析 schema.org Product，缺失时回退到 title/meta。"""

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        product: dict[str, Any] | None = None
        for node in soup.select("script[type='application/ld+json']"):
            try:
                value = json.loads(node.string or node.get_text())
            except json.JSONDecodeError:
                continue
            product = PublicWebEnricher._find_product(value)
            if product:
                break
        brand: str | None = None
        if product:
            raw_brand = product.get("brand")
            brand = raw_brand if isinstance(raw_brand, str) else (raw_brand or {}).get("name")
        description_node = soup.select_one("meta[name='description']")
        warnings: list[str] = []
        result: dict[str, Any] = {
            "source_url": url,
            "title": (product or {}).get("name")
            or (soup.title.string.strip() if soup.title and soup.title.string else None),
            "manufacturer_part_number": (product or {}).get("mpn") or (product or {}).get("sku"),
            "brand": brand,
            "description": (product or {}).get("description")
            or (description_node.get("content") if description_node else None),
            "from_cache": False,
            "warnings": warnings,
        }
        if not result["manufacturer_part_number"]:
            warnings.append("页面未提供标准型号字段，结果必须人工确认")
        return result

    @staticmethod
    def _find_product(value: Any) -> dict[str, Any] | None:
        """在数组或 ``@graph`` 中递归查找 schema.org Product。"""

        if isinstance(value, list):
            return next(
                (found for item in value if (found := PublicWebEnricher._find_product(item))), None
            )
        if not isinstance(value, dict):
            return None
        kinds = value.get("@type", [])
        if kinds == "Product" or (isinstance(kinds, list) and "Product" in kinds):
            return value
        return PublicWebEnricher._find_product(value.get("@graph"))
