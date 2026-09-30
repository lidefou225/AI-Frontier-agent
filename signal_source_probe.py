import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

import requests
from dotenv import load_dotenv
from openai import OpenAI


load_dotenv()

DEFAULT_DAILYHOT_SOURCES = (
    "zhihu",
    "douyin",
    "bilibili",
    "sspai",
    "juejin",
    "hellogithub",
)
DEFAULT_DAILYHOT_BASE_URL = os.getenv(
    "SIGNAL_DAILYHOT_BASE_URL",
    "http://127.0.0.1:3210",
)
DEFAULT_OUTPUT_FILE = Path("data/signal_probe/latest.json")
DEFAULT_MAX_PER_SOURCE = 30
DEFAULT_HN_LIMIT = 40
REQUEST_TIMEOUT = 25
MODEL_RETRIES = 1

AI_PATTERN = re.compile(
    r"(?:"
    r"人工智能|生成式|大模型|语言模型|多模态|智能体|代理模型|机器学习|"
    r"深度学习|神经网络|算力|推理芯片|开源模型|模型训练|"
    r"\bAI\b|\bAGI\b|\bLLM\b|\bagent(?:ic|s)?\b|"
    r"artificial intelligence|machine learning|deep learning|"
    r"language model|foundation model|multimodal|inference|"
    r"OpenAI|Anthropic|Claude|ChatGPT|Gemini|DeepMind|Meta AI|"
    r"Llama|Mistral|Qwen|通义|豆包|DeepSeek|Kimi|MiniMax|"
    r"智谱|GLM|Hugging Face|NVIDIA|英伟达"
    r")",
    re.IGNORECASE,
)


def utc_now():
    return datetime.now(timezone.utc)


def parse_datetime(value):
    if value in (None, ""):
        return None

    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None

    if not isinstance(value, str):
        return None

    cleaned = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_signal(platform, rank, item, captured_at):
    title = str(item.get("title") or "").strip()
    url = str(
        item.get("url")
        or item.get("mobileUrl")
        or item.get("link")
        or ""
    ).strip()
    description = str(
        item.get("desc")
        or item.get("description")
        or ""
    ).strip()
    published_at = parse_datetime(
        item.get("timestamp")
        or item.get("published_at")
        or item.get("time")
    )

    return {
        "signal_id": f"{platform}:{item.get('id', rank)}",
        "platform": platform,
        "rank": rank,
        "title": title,
        "description": description[:800],
        "heat": item.get("hot") or item.get("score") or item.get("points"),
        "url": url,
        "published_at": (
            published_at.isoformat() if published_at else None
        ),
        "captured_at": captured_at.isoformat(),
        "raw_id": str(item.get("id") or ""),
    }


def fetch_dailyhot_source(
    session,
    base_url,
    source,
    max_items,
    captured_at,
):
    endpoint = urljoin(base_url.rstrip("/") + "/", source)
    response = session.get(endpoint, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    payload = response.json()

    if payload.get("code") != 200:
        raise RuntimeError(
            f"DailyHot {source} 返回 code={payload.get('code')}"
        )

    rows = payload.get("data", [])
    if not isinstance(rows, list):
        raise RuntimeError(f"DailyHot {source} data 不是列表")

    signals = []
    for rank, row in enumerate(rows[:max_items], start=1):
        if not isinstance(row, dict):
            continue
        signal = normalize_signal(source, rank, row, captured_at)
        if signal["title"]:
            signals.append(signal)

    return signals, {
        "source": source,
        "status": "ok",
        "count": len(signals),
        "endpoint": endpoint,
        "upstream_updated_at": payload.get("updateTime"),
    }


def fetch_hacker_news(session, limit, captured_at):
    base_url = "https://hacker-news.firebaseio.com/v0/"
    response = session.get(
        urljoin(base_url, "topstories.json"),
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    story_ids = response.json()[:limit]

    def fetch_story(rank, story_id):
        item_response = requests.get(
            urljoin(base_url, f"item/{story_id}.json"),
            timeout=REQUEST_TIMEOUT,
        )
        item_response.raise_for_status()
        item = item_response.json() or {}
        item["hot"] = item.get("score")
        item["timestamp"] = item.get("time")
        item["url"] = item.get("url") or (
            f"https://news.ycombinator.com/item?id={story_id}"
        )
        return rank, normalize_signal(
            "hackernews",
            rank,
            item,
            captured_at,
        )

    signals = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [
            executor.submit(fetch_story, rank, story_id)
            for rank, story_id in enumerate(story_ids, start=1)
        ]
        for future in as_completed(futures):
            _, signal = future.result()
            if signal["title"]:
                signals.append(signal)

    signals.sort(key=lambda row: row["rank"])
    return signals, {
        "source": "hackernews",
        "status": "ok",
        "count": len(signals),
        "endpoint": urljoin(base_url, "topstories.json"),
        "upstream_updated_at": None,
    }


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        if data.strip():
            self.parts.append(data.strip())


class GitHubTrendingParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.repositories = []
        self.current = None
        self.in_h2 = False
        self.in_description = False
        self.in_daily_stars = False

    @staticmethod
    def classes(attributes):
        return dict(attributes).get("class", "").split()

    def handle_starttag(self, tag, attributes):
        classes = self.classes(attributes)
        attrs = dict(attributes)
        if tag == "article" and "Box-row" in classes and self.current is None:
            self.current = {
                "path": "",
                "name_parts": [],
                "description_parts": [],
                "daily_stars_parts": [],
            }
            return
        if self.current is None:
            return

        if tag == "h2":
            self.in_h2 = True
        elif tag == "a" and self.in_h2 and not self.current["path"]:
            self.current["path"] = attrs.get("href", "")
        elif tag == "p":
            self.in_description = True
        elif tag == "span" and "float-sm-right" in classes:
            self.in_daily_stars = True

    def handle_endtag(self, tag):
        if self.current is None:
            return
        if tag == "h2":
            self.in_h2 = False
        elif tag == "p":
            self.in_description = False
        elif tag == "span" and self.in_daily_stars:
            self.in_daily_stars = False
        elif tag == "article":
            self.repositories.append(self.current)
            self.current = None

    def handle_data(self, data):
        if self.current is None or not data.strip():
            return
        cleaned = data.strip()
        if self.in_h2:
            self.current["name_parts"].append(cleaned)
        if self.in_description:
            self.current["description_parts"].append(cleaned)
        if self.in_daily_stars:
            self.current["daily_stars_parts"].append(cleaned)


def strip_html(value):
    parser = TextExtractor()
    parser.feed(value or "")
    return " ".join(parser.parts)


def fetch_hugging_face(session, limit, captured_at):
    endpoint = "https://huggingface.co/api/models"
    response = session.get(
        endpoint,
        params={
            "sort": "trendingScore",
            "direction": "-1",
            "limit": limit,
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()

    signals = []
    for rank, model in enumerate(payload[:limit], start=1):
        model_id = model.get("modelId") or model.get("id") or ""
        tags = ", ".join(model.get("tags") or [])
        signal = normalize_signal(
            "huggingface",
            rank,
            {
                "id": model_id,
                "title": model_id,
                "description": tags,
                "hot": model.get("trendingScore") or model.get("likes"),
                "url": f"https://huggingface.co/{model_id}",
                "published_at": model.get("createdAt"),
            },
            captured_at,
        )
        if signal["title"]:
            signal["metrics"] = {
                "trending_score": model.get("trendingScore"),
                "likes": model.get("likes"),
                "downloads": model.get("downloads"),
            }
            signals.append(signal)

    return signals, {
        "source": "huggingface",
        "status": "ok",
        "count": len(signals),
        "endpoint": endpoint,
        "upstream_updated_at": None,
    }


def fetch_github_trending(session, limit, captured_at):
    endpoint = "https://github.com/trending?since=daily"
    response = session.get(endpoint, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    parser = GitHubTrendingParser()
    parser.feed(response.text)
    repositories = parser.repositories
    if not repositories:
        raise RuntimeError("GitHub Trending 页面未解析到仓库")

    signals = []
    for rank, repository in enumerate(repositories[:limit], start=1):
        repo_path = repository["path"].strip()
        repo_name = repo_path.strip("/").replace(" ", "")
        daily_stars_text = " ".join(repository["daily_stars_parts"])
        daily_stars_match = re.search(r"([\d,]+)\s+stars?\s+today", daily_stars_text)
        daily_stars = (
            int(daily_stars_match.group(1).replace(",", ""))
            if daily_stars_match
            else None
        )
        signal = normalize_signal(
            "github_trending",
            rank,
            {
                "id": repo_name,
                "title": repo_name,
                "description": " ".join(
                    repository["description_parts"]
                ),
                "hot": daily_stars,
                "url": urljoin("https://github.com", repo_path),
            },
            captured_at,
        )
        if signal["title"]:
            signal["metrics"] = {"stars_today": daily_stars}
            signals.append(signal)

    return signals, {
        "source": "github_trending",
        "status": "ok",
        "count": len(signals),
        "endpoint": endpoint,
        "upstream_updated_at": None,
    }


def fetch_product_hunt(session, limit, captured_at):
    endpoint = "https://www.producthunt.com/feed"
    response = session.get(endpoint, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    namespace = {"atom": "http://www.w3.org/2005/Atom"}
    entries = root.findall("atom:entry", namespace)
    if not entries:
        raise RuntimeError("Product Hunt Feed 未解析到产品")

    signals = []
    for rank, entry in enumerate(entries[:limit], start=1):
        title = entry.findtext("atom:title", "", namespace).strip()
        entry_id = entry.findtext("atom:id", title, namespace).strip()
        published = entry.findtext("atom:published", "", namespace).strip()
        content = entry.findtext("atom:content", "", namespace)
        url = ""
        for link in entry.findall("atom:link", namespace):
            if link.get("rel", "alternate") == "alternate":
                url = link.get("href", "")
                break
        signal = normalize_signal(
            "producthunt",
            rank,
            {
                "id": entry_id,
                "title": title,
                "description": strip_html(content),
                "url": url,
                "published_at": published,
            },
            captured_at,
        )
        if signal["title"]:
            signals.append(signal)

    return signals, {
        "source": "producthunt",
        "status": "ok",
        "count": len(signals),
        "endpoint": endpoint,
        "upstream_updated_at": None,
    }


def parse_approx_traffic(value):
    match = re.search(r"([\d,]+)", value or "")
    return int(match.group(1).replace(",", "")) if match else None


def fetch_google_trends(session, limit, captured_at, geo="US"):
    endpoint = f"https://trends.google.com/trending/rss?geo={geo}"
    response = session.get(endpoint, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    items = root.findall("./channel/item")
    if not items:
        raise RuntimeError("Google Trends RSS 未解析到趋势")

    trends_namespace = "https://trends.google.com/trending/rss"
    signals = []
    for rank, item in enumerate(items[:limit], start=1):
        title = item.findtext("title", "").strip()
        published_text = item.findtext("pubDate", "").strip()
        try:
            published = parsedate_to_datetime(published_text).isoformat()
        except (TypeError, ValueError):
            published = None
        traffic_text = item.findtext(
            f"{{{trends_namespace}}}approx_traffic",
            "",
        )
        news_titles = [
            node.text.strip()
            for node in item.findall(
                f"{{{trends_namespace}}}news_item/"
                f"{{{trends_namespace}}}news_item_title"
            )
            if node.text
        ]
        signal = normalize_signal(
            "google_trends",
            rank,
            {
                "id": f"{geo}:{title}",
                "title": title,
                "description": " | ".join(news_titles[:3]),
                "hot": parse_approx_traffic(traffic_text),
                "url": item.findtext("link", endpoint),
                "published_at": published,
            },
            captured_at,
        )
        if signal["title"]:
            signal["metrics"] = {
                "approx_traffic": traffic_text,
                "geo": geo,
            }
            signals.append(signal)

    return signals, {
        "source": "google_trends",
        "status": "ok",
        "count": len(signals),
        "endpoint": endpoint,
        "upstream_updated_at": None,
    }


def within_lookback(signal, captured_at, lookback_hours):
    published_at = parse_datetime(signal.get("published_at"))
    if published_at is None:
        return True
    cutoff = captured_at - timedelta(hours=lookback_hours)
    return cutoff <= published_at <= captured_at + timedelta(minutes=10)


def keyword_candidates(signals):
    candidates = []
    for signal in signals:
        searchable = " ".join(
            [signal.get("title", ""), signal.get("description", "")]
        )
        if AI_PATTERN.search(searchable):
            candidates.append(signal)
    return candidates


def parse_json_response(content):
    cleaned = (content or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


def classify_candidates(candidates, api_key):
    if not candidates:
        return []

    if not api_key:
        return [
            {
                **signal,
                "ai_relevant": True,
                "event_key": signal["title"],
                "entity": "",
                "category": "unclassified",
                "filter_reason": "关键词命中；未配置 DEEPSEEK_API_KEY",
            }
            for signal in candidates
        ]

    client = OpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com",
    )
    model_input = [
        {
            "index": index,
            "platform": signal["platform"],
            "rank": signal["rank"],
            "title": signal["title"],
            "description": signal["description"][:300],
        }
        for index, signal in enumerate(candidates)
    ]
    prompt = f"""
你是 AI Frontier 的热点信号筛选员。请判断以下热榜条目是否直接涉及 AI 技术、模型、Agent、算力、开发工具、真实采用或 AI 产业事件。

规则：
1. 仅仅出现“像 AI”“AI 味”等比喻不算 AI 事件。
2. 普通科技、芯片、互联网公司新闻，若没有直接 AI 关系，应拒绝。
3. AI 生成娱乐内容可以标记 relevant，但 category 必须是 ai_entertainment。
4. event_key 用稳定、简短的中文概括同一事件，使跨平台相同事件尽量得到完全一致的 event_key。
5. 不得补充输入中没有的事实。

只输出严格 JSON：
{{
  "results": [
    {{
      "index": 0,
      "relevant": true,
      "event_key": "某个人 AI Agent 下载与采用增长",
      "entity": "某个人 AI Agent",
      "category": "agent_adoption",
      "reason": "直接涉及个人 AI Agent 的采用"
    }}
  ]
}}

待筛选条目：
{json.dumps(model_input, ensure_ascii=False)}
"""
    last_error = None
    for attempt in range(MODEL_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model="deepseek-flash",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                max_tokens=3000,
                extra_body={"thinking": {"type": "disabled"}},
            )
            payload = parse_json_response(
                response.choices[0].message.content
            )
            break
        except Exception as error:
            last_error = error
            if attempt >= MODEL_RETRIES:
                raise RuntimeError(
                    f"AI 热点筛选失败：{last_error}"
                ) from error
            time.sleep(1)

    review_by_index = {
        review.get("index"): review
        for review in payload.get("results", [])
        if isinstance(review, dict)
        and isinstance(review.get("index"), int)
    }
    classified = []
    for index, signal in enumerate(candidates):
        review = review_by_index.get(index)
        if not review or review.get("relevant") is not True:
            continue
        event_key = str(
            review.get("event_key") or signal["title"]
        ).strip()
        classified.append(
            {
                **signal,
                "ai_relevant": True,
                "event_key": event_key,
                "entity": str(review.get("entity") or "").strip(),
                "category": str(
                    review.get("category") or "other_ai"
                ).strip(),
                "filter_reason": str(
                    review.get("reason") or ""
                ).strip(),
            }
        )
    return classified


GENERIC_EVENT_ENTITIES = {
    "ai",
    "agent",
    "llm",
    "人工智能",
    "大模型",
    "智能体",
    "ai开发",
}


def normalize_event_text(value):
    return re.sub(
        r"[^a-z0-9\u4e00-\u9fff]+",
        "",
        (value or "").casefold(),
    )


def event_group_key(signal):
    category = signal.get("category", "other_ai")
    entity = normalize_event_text(signal.get("entity", ""))
    if entity and entity not in GENERIC_EVENT_ENTITIES and len(entity) >= 3:
        return f"{category}:entity:{entity}"

    event_key = normalize_event_text(
        signal.get("event_key") or signal["title"]
    )
    return f"{category}:event:{event_key or signal['signal_id']}"


def group_events(classified_signals):
    grouped = defaultdict(list)
    for signal in classified_signals:
        grouped[event_group_key(signal)].append(signal)

    events = []
    for aggregation_key, group in grouped.items():
        group.sort(key=lambda row: (row["rank"], row["platform"]))
        platforms = sorted({row["platform"] for row in group})
        best_rank = min(row["rank"] for row in group)
        event = {
            "event_key": group[0]["event_key"],
            "aggregation_key": aggregation_key,
            "aggregation_basis": (
                "same_entity_and_category"
                if ":entity:" in aggregation_key
                else "same_event_key"
            ),
            "entity": next(
                (
                    row.get("entity", "")
                    for row in group
                    if row.get("entity")
                ),
                "",
            ),
            "category": Counter(
                row.get("category", "other_ai")
                for row in group
            ).most_common(1)[0][0],
            "platforms": platforms,
            "platform_count": len(platforms),
            "best_rank": best_rank,
            "signal_count": len(group),
            "signal_score": len(platforms) * 100 + max(0, 50 - best_rank),
            "signals": group,
        }
        events.append(event)

    events.sort(
        key=lambda row: (
            -row["signal_score"],
            row["best_rank"],
            row["event_key"],
        )
    )
    return events


def collect_signals(args):
    captured_at = utc_now()
    session = requests.Session()
    session.headers.update(
        {"User-Agent": "AI-Frontier-Signal-Probe/0.1"}
    )
    signals = []
    source_status = []

    for source in args.sources:
        try:
            rows, status = fetch_dailyhot_source(
                session,
                args.dailyhot_base_url,
                source,
                args.max_per_source,
                captured_at,
            )
            signals.extend(rows)
            source_status.append(status)
            print(f"[成功] {source}: {len(rows)} 条")
        except Exception as error:
            source_status.append(
                {
                    "source": source,
                    "status": "failed",
                    "count": 0,
                    "endpoint": urljoin(
                        args.dailyhot_base_url.rstrip("/") + "/",
                        source,
                    ),
                    "error": str(error),
                }
            )
            print(f"[失败] {source}: {error}")

    if not args.skip_hacker_news:
        try:
            rows, status = fetch_hacker_news(
                session,
                args.hn_limit,
                captured_at,
            )
            signals.extend(rows)
            source_status.append(status)
            print(f"[成功] hackernews: {len(rows)} 条")
        except Exception as error:
            source_status.append(
                {
                    "source": "hackernews",
                    "status": "failed",
                    "count": 0,
                    "endpoint": (
                        "https://hacker-news.firebaseio.com/v0/"
                        "topstories.json"
                    ),
                    "error": str(error),
                }
            )
            print(f"[失败] hackernews: {error}")

    if not args.skip_overseas:
        overseas_fetchers = (
            (
                "huggingface",
                lambda: fetch_hugging_face(
                    session,
                    args.overseas_limit,
                    captured_at,
                ),
            ),
            (
                "github_trending",
                lambda: fetch_github_trending(
                    session,
                    args.overseas_limit,
                    captured_at,
                ),
            ),
            (
                "producthunt",
                lambda: fetch_product_hunt(
                    session,
                    args.overseas_limit,
                    captured_at,
                ),
            ),
            (
                "google_trends",
                lambda: fetch_google_trends(
                    session,
                    args.overseas_limit,
                    captured_at,
                    args.google_trends_geo,
                ),
            ),
        )
        for source, fetcher in overseas_fetchers:
            try:
                rows, status = fetcher()
                signals.extend(rows)
                source_status.append(status)
                print(f"[成功] {source}: {len(rows)} 条")
            except Exception as error:
                source_status.append(
                    {
                        "source": source,
                        "status": "failed",
                        "count": 0,
                        "endpoint": None,
                        "error": str(error),
                    }
                )
                print(f"[失败] {source}: {error}")

    recent_signals = [
        signal
        for signal in signals
        if within_lookback(
            signal,
            captured_at,
            args.lookback_hours,
        )
    ]
    candidates = keyword_candidates(recent_signals)
    classified = classify_candidates(
        candidates,
        os.getenv("DEEPSEEK_API_KEY"),
    )
    events = group_events(classified)

    return {
        "generated_at": captured_at.isoformat(),
        "lookback_hours": args.lookback_hours,
        "mode": "signal_source_probe",
        "source_status": source_status,
        "counts": {
            "raw_signals": len(signals),
            "signals_in_window": len(recent_signals),
            "keyword_candidates": len(candidates),
            "ai_signals": len(classified),
            "events": len(events),
        },
        "events": events,
    }


def print_summary(report, max_events):
    counts = report["counts"]
    print("\n=== AI Frontier 信号源验证结果 ===")
    print(
        f"原始信号 {counts['raw_signals']} 条；"
        f"24h 窗口 {counts['signals_in_window']} 条；"
        f"关键词候选 {counts['keyword_candidates']} 条；"
        f"AI 信号 {counts['ai_signals']} 条；"
        f"聚合事件 {counts['events']} 个。"
    )

    for index, event in enumerate(
        report["events"][:max_events],
        start=1,
    ):
        platforms = ", ".join(event["platforms"])
        print(
            f"\n{index}. {event['event_key']}\n"
            f"   平台：{platforms}｜最佳排名：{event['best_rank']}｜"
            f"信号数：{event['signal_count']}"
        )
        for signal in event["signals"][:3]:
            print(
                f"   - [{signal['platform']} #{signal['rank']}] "
                f"{signal['title']}"
            )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="验证聚合热榜能否稳定发现过去 24 小时的 AI 热点候选。"
    )
    parser.add_argument(
        "--dailyhot-base-url",
        default=DEFAULT_DAILYHOT_BASE_URL,
    )
    parser.add_argument(
        "--sources",
        default=",".join(DEFAULT_DAILYHOT_SOURCES),
        help="DailyHot 路由，逗号分隔。",
    )
    parser.add_argument(
        "--max-per-source",
        type=int,
        default=DEFAULT_MAX_PER_SOURCE,
    )
    parser.add_argument(
        "--hn-limit",
        type=int,
        default=DEFAULT_HN_LIMIT,
    )
    parser.add_argument(
        "--skip-hacker-news",
        action="store_true",
    )
    parser.add_argument(
        "--skip-overseas",
        action="store_true",
        help="跳过 Hugging Face、GitHub Trending、Product Hunt 和 Google Trends。",
    )
    parser.add_argument(
        "--overseas-limit",
        type=int,
        default=30,
    )
    parser.add_argument(
        "--google-trends-geo",
        default="US",
    )
    parser.add_argument(
        "--lookback-hours",
        type=int,
        default=24,
    )
    parser.add_argument(
        "--max-events",
        type=int,
        default=20,
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_FILE,
    )
    args = parser.parse_args(argv)
    args.sources = tuple(
        source.strip()
        for source in args.sources.split(",")
        if source.strip()
    )
    return args


def main(argv=None):
    args = parse_args(argv)
    report = collect_signals(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print_summary(report, args.max_events)
    print(f"\n完整结果：{args.output}")

    if not any(
        status.get("status") == "ok"
        for status in report["source_status"]
    ):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
