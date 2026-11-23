#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jenning.ipv64.de 数据自动保鲜脚本（只用 Python 标准库）

做三件事：
1. TestFlight 名额：逐个打开 data.json 里的 TF 公开链接，满了 / 停止收人 / 链接失效的条目剔除；
   网络超时等临时异常一律保留，避免误删。
2. App Store 价格：按条目自己的 region（默认 CN）调用 Apple 官方 lookup 接口
   （country=cn / country=us）核实 type=free / deal 的条目，
   限免已恢复原价、降价已涨回的剔除，仍在优惠的更新现价。
3. 新条目：扫描公开源里明确写了「限免 / 限时免费 / free」并且带 App Store ID 的条目，
   再用 Apple 接口核实（必须是 iOS App、对应区服在售、现价 0）才收录；
   源不稳或没新东西就安静跳过。

最后把 updated_at 写成当前北京时间并写回 data.json。
用法：python3 scripts/update.py [--dry-run]
"""

import datetime
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_PATH = os.path.join(ROOT, "data.json")

UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
)
UA_DESKTOP = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
TIMEOUT = 25
BEIJING = datetime.timezone(datetime.timedelta(hours=8))

TYPE_ORDER = {"tf": 0, "free": 1, "deal": 2}

# 区服配置：Apple lookup 的 country 参数、货币代码与展示符号
REGIONS = {
    "CN": {"country": "cn", "currency": "CNY", "symbol": "¥", "label": "国区"},
    "US": {"country": "us", "currency": "USD", "symbol": "$", "label": "美区"},
}
DEFAULT_REGION = "CN"

# TestFlight 页面状态判定
TF_FULL_MARKERS = [
    "this beta is full",
    "isn't accepting any new testers",
    "is not accepting any new testers",
    "not accepting any new testers",
    "this beta is closed",
]
TF_OPEN_MARKERS = ["view in testflight", "start testing"]


def log(msg):
    print(msg, flush=True)


def fetch(url, ua=UA_MOBILE, timeout=TIMEOUT):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.status, raw.decode(charset, "replace")


def region_of(item):
    region = (item.get("region") or DEFAULT_REGION).upper()
    return region if region in REGIONS else DEFAULT_REGION


# ---------------------------------------------------------------- TestFlight


def check_testflight(url):
    """返回 (状态, 说明)。状态：open / full / gone / unknown / error"""
    try:
        status, html = fetch(url)
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            return "gone", "HTTP %s" % exc.code
        return "error", "HTTP %s" % exc.code
    except Exception as exc:  # 超时、DNS、TLS 等：临时异常，保留条目
        return "error", type(exc).__name__

    low = html.lower()
    for marker in TF_FULL_MARKERS:
        if marker in low:
            return "full", marker
    for marker in TF_OPEN_MARKERS:
        if marker in low:
            return "open", marker
    return "unknown", "no status marker"


# ---------------------------------------------------------------- App Store


def lookup_app(app_id, region=DEFAULT_REGION, fresh=True):
    """按区服查 Apple lookup，返回 (状态, data)。状态：ok / notfound / error

    每次都带一个随机参数：Apple 的 lookup 结果在 CDN 上缓存得很久，
    同一 App 不加参数会拿到几小时前的旧价格（实测有 App 显示限免、
    实际已恢复 $14.99，也有降价中的 App 被旧缓存显示成原价）。
    加参数等于强制回源，拿到的才是当前价。
    """
    country = REGIONS[region]["country"]
    url = "https://itunes.apple.com/lookup?country=%s&id=%s" % (country, app_id)
    if fresh:
        url += "&cb=%d" % random.randint(100000, 999999999)
    try:
        _, body = fetch(url, ua=UA_DESKTOP)
        payload = json.loads(body)
    except Exception as exc:
        return "error", {"error": type(exc).__name__}
    results = payload.get("results") or []
    if not results:
        return "notfound", {}
    return "ok", results[0]


def tidy_price(value):
    if value is None:
        return None
    return int(value) if float(value).is_integer() else round(float(value), 2)


def app_price(entry):
    """取现价（数值，所在区货币），取不到返回 None"""
    for key in ("price", "priceFormatted"):
        val = entry.get(key)
        if isinstance(val, (int, float)):
            return tidy_price(val)
    raw = entry.get("priceFormatted") or ""
    match = re.search(r"[\d.]+", str(raw).replace(",", ""))
    if match:
        try:
            return tidy_price(float(match.group(0)))
        except ValueError:
            return None
    return None


def money(value, region):
    """给日志用的价格展示，例如 ¥38 / $4.99 / 0"""
    symbol = REGIONS[region]["symbol"]
    if value is None:
        return "?"
    if float(value) == 0:
        return "%s0" % symbol
    return "%s%g" % (symbol, float(value))


def app_id_from_url(url):
    match = re.search(r"/id(\d{6,})", url or "")
    return int(match.group(1)) if match else None


def is_ios_app(entry):
    kind = (entry.get("kind") or "").lower()
    if kind and kind != "software":
        return False
    devices = entry.get("supportedDevices") or []
    if devices and not any(d.startswith(("iPhone", "iPad", "iPod")) for d in devices):
        return False
    return True


def clean_text(text, limit=110):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip("，。、；,. ") + "…"
    return text


def plat_list(entry):
    devices = " ".join(entry.get("supportedDevices") or [])
    plats = []
    if "iPhone" in devices or "iPod" in devices or not devices:
        plats.append("iPhone")
    if "iPad" in devices:
        plats.append("iPad")
    return plats or ["iPhone"]


# ---------------------------------------------------------------- 发现新条目


FEEDS = [
    ("CN", "appinn", "https://www.appinn.com/feed/"),
    ("CN", "apprcn", "https://free.apprcn.com/feed/"),
    ("CN", "applealmond", "https://www.applealmond.com/feed"),
]
# 美区限免：360 Reader 每天更新的「Paid iPhone Apps Gone Free」表格，
# 表里直接给出 App Store 链接（含 id）、分类、原价与评分，解析稳定。
US_FREE_PAGE = "https://360-reader.com/paid-iphone-apps-gone-free/"

DEAL_WORDS = ("限免", "限时免费", "限時免費", "限免中", "免费领取", "免費領取")
ORIG_PRICE_RE = re.compile(r"(?:原价|原價|定价|定價|原价\s*[:：]?)\s*[¥￥$]\s*(\d+(?:\.\d+)?)")
APPLE_ID_RE = re.compile(r"apps\.apple\.com/[a-z]{2}/app/[^\s\"'<>]*?/id(\d{6,})")


def feed_entries(xml):
    """把 RSS 的 <item> / Atom 的 <entry> 粗略拆开"""
    blocks = re.findall(r"<item[ >].*?</item>", xml, re.S | re.I)
    if not blocks:
        blocks = re.findall(r"<entry[ >].*?</entry>", xml, re.S | re.I)
    return blocks


def strip_cdata(text):
    return re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", text or "", flags=re.S)


def discover_from_feeds(known_ids, seen):
    """从国区公开 RSS 里找「明确限免 + 带 App Store ID」的候选"""
    out = []
    for region, name, url in FEEDS:
        try:
            _, xml = fetch(url, ua=UA_DESKTOP)
        except Exception as exc:
            log("  [源] %s 抓取失败（%s），跳过" % (name, type(exc).__name__))
            continue
        blocks = feed_entries(xml)
        if not blocks:
            log("  [源] %s 没有解析到条目，跳过" % name)
            continue
        hits = 0
        for block in blocks:
            text = strip_cdata(block)
            plain = re.sub(r"<[^>]+>", " ", text)
            plain = re.sub(r"\s+", " ", plain)
            if not any(word in plain for word in DEAL_WORDS):
                continue
            for app_id in APPLE_ID_RE.findall(text):
                app_id = int(app_id)
                if app_id in known_ids or app_id in seen:
                    continue
                title = re.search(r"<title[^>]*>(.*?)</title>", text, re.S)
                was = ORIG_PRICE_RE.search(plain)
                seen.add(app_id)
                out.append(
                    {
                        "app_id": app_id,
                        "name_hint": clean_text(strip_cdata(title.group(1)) if title else "", 60),
                        "was": float(was.group(1)) if was else None,
                        "feed": name,
                        "region": region,
                    }
                )
                hits += 1
        log("  [源] %s：%d 条 RSS，%d 个新 App ID" % (name, len(blocks), hits))
        if len(out) >= 5:
            break
    return out


RATING_RE = re.compile(r"([\d.]+)\s*\(([\d,]+)")


def discover_from_us_page(known_ids, seen):
    """解析 360 Reader 的美区限免表格：名字 / 分类 / 原价 Free / 评分 / App Store id"""
    try:
        _, html = fetch(US_FREE_PAGE, ua=UA_DESKTOP)
    except Exception as exc:
        log("  [源] 360read 抓取失败（%s），跳过" % type(exc).__name__)
        return []
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I)
    out = []
    for row in rows:
        if "apps.apple.com" not in row:
            continue
        cells = [re.sub(r"<[^>]+>", " ", c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]
        cells = [re.sub(r"\s+", " ", c).strip() for c in cells]
        if len(cells) < 3:
            continue
        price_cell = cells[2]
        if "free" not in price_cell.lower():
            continue
        was = re.search(r"\$\s*(\d+(?:\.\d+)?)", price_cell)
        id_match = re.search(r"apps\.apple\.com/us/app/[^\s\"'<>]*?/id(\d{6,})", row)
        if not id_match:
            continue
        app_id = int(id_match.group(1))
        if app_id in known_ids or app_id in seen:
            continue
        rating = None
        rating_count = 0
        rating_cell = cells[3] if len(cells) > 3 else ""
        match = RATING_RE.search(rating_cell.replace(",", ""))
        if match:
            try:
                rating = float(match.group(1))
                rating_count = int(match.group(2))
            except ValueError:
                rating = None
        # 美区源里杂项很多，只收有足够人打分、评分够高的，避免把站点变成流水账
        if rating is None or rating < 4.5 or rating_count < 300:
            continue
        seen.add(app_id)
        out.append(
            {
                "app_id": app_id,
                "name_hint": clean_text(cells[0], 60),
                "was": float(was.group(1)) if was else None,
                "feed": "360read",
                "region": "US",
                "rating": rating,
                "rating_count": rating_count,
            }
        )
    log("  [源] 360read：%d 行表格，%d 个新美区 App ID" % (len(rows), len(out)))
    out.sort(key=lambda c: -(c.get("rating_count") or 0))
    return out


def build_item(cand, entry):
    """把 Apple 接口返回的条目变成 data.json 里的一个免单/降价条目"""
    region = cand.get("region") or DEFAULT_REGION
    cfg = REGIONS[region]
    price_was = cand.get("was")
    price_now = app_price(entry) or 0.0
    name = clean_text(entry.get("trackName") or cand.get("name_hint") or "", 60)
    genre = clean_text(entry.get("primaryGenreName") or "", 20)
    if region == "US":
        # 美区应用的描述多是英文，站点用中文，这里写一句中文通用说明
        rating = entry.get("averageUserRating")
        votes = entry.get("userRatingCount") or 0
        extra = ""
        if rating and votes:
            extra = "，%.1f★ / %d 个评分" % (float(rating), int(votes))
        if price_was and price_was > price_now:
            desc = "%s 类应用，美区限时免费（原价 %s%g%s），价格由 Apple 官方接口核实。" % (
                genre or "App Store",
                cfg["symbol"],
                price_was,
                extra,
            )
        else:
            desc = "%s 类应用，Apple 官方接口核实美区现价 %s0（限时免费%s）。" % (
                genre or "App Store",
                cfg["symbol"],
                extra,
            )
    else:
        desc = clean_text(entry.get("description") or "", 110)
        if not desc:
            desc = "%s 上的 iOS 应用，用 Apple 官方接口核实当前在中国区%s。" % (
                genre or "App Store",
                "免费" if price_now == 0 else "售价 %s%g" % (cfg["symbol"], price_now),
            )
    url = entry.get("trackViewUrl") or "https://apps.apple.com/%s/app/id%s" % (cfg["country"], cand["app_id"])
    url = re.sub(r"\?.*$", "", url)
    item = {
        "type": "free",
        "name": name,
        "desc": desc,
        "badge": "限免中",
        "plat": plat_list(entry),
        "region": region,
        "currency": cfg["currency"],
        "url": url,
        "app_id": int(cand["app_id"]),
        "price_now": price_now,
        "auto": True,
    }
    if price_was and price_was > price_now:
        item["price_was"] = price_was
    return item


# ---------------------------------------------------------------- 主流程


def judge_deal(it, entry, region):
    """判断条目还能不能留：返回 (keep|drop, 现价, 剔除原因)"""
    if entry is None:
        return "drop", None, "%s App Store 查不到该 App" % REGIONS[region]["label"]
    if not is_ios_app(entry):
        return "drop", None, "已不是 iOS App"
    price = app_price(entry)
    if price is None:
        return "keep", None, None
    if it.get("type") == "free":
        if price != 0:
            return "drop", price, "限免已结束，现价 %s" % money(price, region)
        return "keep", 0, None
    was = it.get("price_was")
    if was is not None and price >= float(was):
        return "drop", price, "降价已结束，现价 %s（原价 %s）" % (money(price, region), money(was, region))
    return "keep", price, None


def main():
    dry_run = "--dry-run" in sys.argv

    with open(DATA_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    items = [it for it in data.get("items", []) if isinstance(it, dict)]
    before = len(items)
    log("读取 %d 条条目" % before)

    # 老数据没有 region / currency 时补成国区
    for it in items:
        if it.get("type") in ("free", "deal"):
            it["region"] = region_of(it)
            it["currency"] = REGIONS[it["region"]]["currency"]

    # ---- 1 & 2：并发核实 ----
    tf_items = [it for it in items if it.get("type") == "tf"]
    store_items = [it for it in items if it.get("type") in ("free", "deal") and it.get("app_id")]

    tf_results = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(check_testflight, it["url"]): it for it in tf_items if it.get("url")}
        for fut, it in futures.items():
            try:
                tf_results[it["url"]] = fut.result()
            except Exception as exc:
                tf_results[it["url"]] = ("error", type(exc).__name__)

    store_results = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(lookup_app, it["app_id"], region_of(it)): it for it in store_items}
        for fut, it in futures.items():
            store_results[id(it)] = fut.result()
    for it in store_items:
        if id(it) not in store_results:
            store_results[id(it)] = ("error", {"error": "no result"})

    kept = []
    dropped = []

    for it in items:
        kind = it.get("type")

        if kind == "tf":
            status, detail = tf_results.get(it.get("url"), ("unknown", "no url"))
            if status in ("full", "gone"):
                dropped.append((it, "TF %s（%s）" % ("已满/停止收人" if status == "full" else "链接失效", detail)))
                continue
            if status == "unknown":
                log("  [TF] 保留（页面状态不明）：%s" % it.get("name"))
            kept.append(it)
            continue

        if kind in ("free", "deal") and it.get("app_id"):
            region = region_of(it)
            cfg = REGIONS[region]
            status, entry = store_results.get(id(it), ("error", {}))
            if status == "error":
                log("  [价格] 接口异常，保留：%s" % it.get("name"))
                kept.append(it)
                continue

            decision, price, reason = judge_deal(it, status == "ok" and entry or None, region)

            # 要剔除之前再查几次（带随机参数绕缓存）：Apple CDN 偶尔还会返回没刷新价格的旧数据，
            # 只凭一次结果就删条目，会误删仍在限免的 App。
            if decision == "drop":
                for attempt in range(3):
                    time.sleep(1.2)
                    status2, entry2 = lookup_app(it["app_id"], region, fresh=True)
                    if status2 != "ok":
                        continue
                    decision2, price2, reason2 = judge_deal(it, entry2, region)
                    log(
                        "  [复核 %d/3] %s：%s %s"
                        % (attempt + 1, it.get("name"), status2, money(app_price(entry2), region))
                    )
                    if decision2 == "keep":
                        decision, price, reason, status, entry = "keep", price2, None, status2, entry2
                        break
                    decision, price, reason = decision2, price2, reason2

            if decision == "drop":
                dropped.append((it, reason))
                continue
            if price is None:
                log("  [价格] 读不到价格，保留：%s" % it.get("name"))
                kept.append(it)
                continue

            if it["type"] == "deal" and price <= 0:
                # 降价变成免费了：升级成限免
                it["type"] = "free"
                it["badge"] = "限免中"
            it["price_now"] = price
            if it.get("url") and entry.get("trackViewUrl"):
                it["url"] = re.sub(r"\?.*$", "", entry["trackViewUrl"])
            if not it.get("name"):
                it["name"] = clean_text(entry.get("trackName") or "", 60)
            kept.append(it)
            continue

        kept.append(it)

    # ---- 3：补充新条目 ----
    log("扫描公开源寻找新条目…")
    known_ids = {it.get("app_id") for it in kept if it.get("app_id")}
    seen = set()
    candidates = []
    for discover in (discover_from_feeds, discover_from_us_page):
        try:
            candidates.extend(discover(known_ids, seen))
        except Exception as exc:
            log("  发现流程出错（%s），跳过该源" % type(exc).__name__)

    added = []
    for cand in candidates:
        if len(added) >= 3:
            break
        region = cand.get("region") or DEFAULT_REGION
        status, entry = lookup_app(cand["app_id"], region)
        if status != "ok":
            log("  跳过 %s：Apple 接口 %s" % (cand["app_id"], status))
            continue
        if not is_ios_app(entry):
            log("  跳过 %s：不是 iOS App" % cand["app_id"])
            continue
        price = app_price(entry)
        if price is None or price != 0:
            log("  跳过 %s：%s现价不是 0（%s）" % (cand["app_id"], REGIONS[region]["label"], money(price, region)))
            continue
        if any(it.get("app_id") == cand["app_id"] for it in kept):
            continue
        item = build_item(cand, entry)
        kept.append(item)
        added.append(item)
        log("  + 新增[%s]：%s（%s）" % (REGIONS[region]["label"], item["name"], item["url"]))

    for it in kept:
        it.pop("auto", None)

    # 排序：tf → free → deal，组内保持原顺序（同一类型里国区在前、美区在后）
    kept.sort(key=lambda it: (TYPE_ORDER.get(it.get("type"), 9), 0 if region_of(it) == "CN" else 1))

    # 安全阀：一次剔除过半条目通常意味着上游接口异常，宁可不更新也不要清空站点
    if before >= 10 and len(dropped) > before // 2:
        log("警告：本次要剔除 %d/%d 条，超过一半，疑似接口异常，放弃写回。" % (len(dropped), before))
        return 0
    if not kept:
        log("警告：核对后没有任何条目，放弃写回。")
        return 0

    now = datetime.datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M")
    data["updated_at"] = now
    data["items"] = kept

    log("")
    log(
        "结果：原有 %d 条 → 保留 %d 条，剔除 %d 条，新增 %d 条，共 %d 条"
        % (before, len(kept) - len(added), len(dropped), len(added), len(kept))
    )
    for it, reason in dropped:
        log("  - 剔除 %s：%s" % (it.get("name"), reason))
    log("updated_at = %s（北京时间）" % now)

    if dry_run:
        log("--dry-run：不写入文件")
        return 0

    tmp = DATA_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, DATA_PATH)
    log("已写回 %s" % os.path.relpath(DATA_PATH, ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
