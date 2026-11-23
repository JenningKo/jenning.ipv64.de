#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jenning.ipv64.de 数据自动保鲜脚本（只用 Python 标准库）

做三件事：
1. TestFlight 名额：逐个打开 data.json 里的 TF 公开链接，满了 / 停止收人 / 链接失效的条目剔除；
   网络超时等临时异常一律保留，避免误删。
2. App Store 价格：用 Apple 官方 lookup 接口（中国区）核实 type=free / deal 的条目，
   限免已恢复原价、降价已涨回的剔除，仍在优惠的更新现价。
3. 新条目：扫描公开 RSS 源里明确写了「限免 / 限时免费」并且带 App Store ID 的条目，
   再用 Apple 接口核实（必须是 iOS App、中国区在售、现价 0 元）才收录；源不稳或没新东西就安静跳过。

最后把 updated_at 写成当前北京时间并写回 data.json。
用法：python3 scripts/update.py [--dry-run]
"""

import datetime
import json
import os
import re
import sys
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


# ---------------------------------------------------------------- TestFlight


def check_testflight(url):
    """返回 (状态, 说明)。状态：open / full / gone / unknown / error"""
    try:
        status, html = fetch(url)
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            return "gone", "HTTP %s" % exc.code
        if exc.code in (429, 500, 502, 503, 504):
            return "error", "HTTP %s" % exc.code
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


def lookup_app(app_id):
    """返回 (状态, data)。状态：ok / notfound / error"""
    url = "https://itunes.apple.com/lookup?country=cn&id=%s" % app_id
    try:
        _, body = fetch(url, ua=UA_DESKTOP)
        payload = json.loads(body)
    except Exception as exc:
        return "error", {"error": type(exc).__name__}
    results = payload.get("results") or []
    if not results:
        return "notfound", {}
    return "ok", results[0]


def app_price(entry):
    """取现价（数值，人民币），取不到返回 None"""
    for key in ("price", "priceFormatted"):
        val = entry.get(key)
        if isinstance(val, (int, float)):
            return float(val)
    raw = entry.get("priceFormatted") or ""
    match = re.search(r"[\d.]+", str(raw).replace(",", ""))
    if match:
        try:
            return float(match.group(0))
        except ValueError:
            return None
    return None


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
    ("appinn", "https://www.appinn.com/feed/"),
    ("apprcn", "https://free.apprcn.com/feed/"),
    ("applealmond", "https://www.applealmond.com/feed"),
]
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


def discover_candidates(data):
    """从公开 RSS 里找「明确限免 + 带 App Store ID」的候选，返回 [{app_id, name_hint, was, feed}]"""
    known_ids = {it.get("app_id") for it in data["items"] if it.get("app_id")}
    seen = set()
    out = []
    for name, url in FEEDS:
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
                    }
                )
                hits += 1
        log("  [源] %s：%d 条 RSS，%d 个新 App ID" % (name, len(blocks), hits))
        if len(out) >= 5:
            break
    return out


def build_item(cand, entry):
    """把 Apple 接口返回的条目变成 data.json 里的一个免单/降价条目"""
    price_was = cand.get("was")
    price_now = app_price(entry) or 0.0
    name = clean_text(entry.get("trackName") or cand.get("name_hint") or "", 60)
    genre = clean_text(entry.get("primaryGenreName") or "", 20)
    desc = clean_text(entry.get("description") or "", 110)
    if not desc:
        desc = "%s 上的 iOS 应用，用 Apple 官方接口核实当前在中国区%s。" % (
            genre or "App Store",
            "免费" if price_now == 0 else "售价 ¥%g" % price_now,
        )
    url = entry.get("trackViewUrl") or "https://apps.apple.com/cn/app/id%s" % cand["app_id"]
    url = re.sub(r"\?.*$", "", url)
    item = {
        "type": "free",
        "name": name,
        "desc": desc,
        "badge": "限免中",
        "plat": plat_list(entry),
        "url": url,
        "app_id": int(cand["app_id"]),
        "price_now": price_now,
        "auto": True,
    }
    if price_was and price_was > price_now:
        item["price_was"] = price_was
    return item


# ---------------------------------------------------------------- 主流程


def main():
    dry_run = "--dry-run" in sys.argv

    with open(DATA_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    items = [it for it in data.get("items", []) if isinstance(it, dict)]
    before = len(items)
    log("读取 %d 条条目" % before)

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
        futures = {pool.submit(lookup_app, it["app_id"]): it for it in store_items}
        for fut, it in futures.items():
            try:
                store_results[it["app_id"]] = fut.result()
            except Exception as exc:
                store_results[it["app_id"]] = ("error", {"error": type(exc).__name__})

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
            status, entry = store_results.get(it["app_id"], ("error", {}))
            if status == "error":
                log("  [价格] 接口异常，保留：%s" % it.get("name"))
                kept.append(it)
                continue
            if status == "notfound":
                dropped.append((it, "中国区 App Store 查不到该 App"))
                continue
            if not is_ios_app(entry):
                dropped.append((it, "已不是 iOS App"))
                continue
            price = app_price(entry)
            if price is None:
                log("  [价格] 读不到价格，保留：%s" % it.get("name"))
                kept.append(it)
                continue
            was = it.get("price_was")
            if kind == "free":
                if price != 0:
                    dropped.append((it, "限免已结束，现价 ¥%g" % price))
                    continue
                it["price_now"] = 0
            else:  # deal
                if was is not None and price >= float(was):
                    dropped.append((it, "降价已结束，现价 ¥%g（原价 ¥%g）" % (price, float(was))))
                    continue
                if was is not None and price <= 0:
                    # 降价变成免费了：升级成限免
                    it["type"] = "free"
                    it["badge"] = "限免中"
                it["price_now"] = price
            if it.get("url") and entry.get("trackViewUrl"):
                it["url"] = re.sub(r"\?.*$", "", entry["trackViewUrl"])
            name = entry.get("trackName")
            if name:
                it["name"] = clean_text(name, 60)
            kept.append(it)
            continue

        kept.append(it)

    # ---- 3：补充新条目 ----
    log("扫描公开源寻找新条目…")
    added = []
    try:
        candidates = discover_candidates({"items": kept})
    except Exception as exc:
        candidates = []
        log("  发现流程出错（%s），跳过补充" % type(exc).__name__)

    for cand in candidates:
        if len(added) >= 3:
            break
        status, entry = lookup_app(cand["app_id"])
        if status != "ok":
            log("  跳过 %s：Apple 接口 %s" % (cand["app_id"], status))
            continue
        if not is_ios_app(entry):
            log("  跳过 %s：不是 iOS App" % cand["app_id"])
            continue
        price = app_price(entry)
        if price is None or price != 0:
            log("  跳过 %s：现价不是 0（%s）" % (cand["app_id"], price))
            continue
        if any(it.get("app_id") == cand["app_id"] for it in kept):
            continue
        item = build_item(cand, entry)
        kept.append(item)
        added.append(item)
        log("  + 新增：%s（%s）" % (item["name"], item["url"]))

    for it in kept:
        it.pop("auto", None)

    # 排序：tf → free → deal，组内保持原顺序
    kept.sort(key=lambda it: TYPE_ORDER.get(it.get("type"), 9))

    now = datetime.datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M")
    data["updated_at"] = now
    data["items"] = kept

    log("")
    log("结果：原有 %d 条 → 保留 %d 条，剔除 %d 条，新增 %d 条，共 %d 条"
        % (before, len(kept) - len(added), len(dropped), len(added), len(kept)))
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
