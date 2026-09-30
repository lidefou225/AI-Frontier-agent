import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from dotenv import load_dotenv
from openai import OpenAI
from tavily import TavilyClient


load_dotenv()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
FEISHU_WEBHOOK_URL = os.getenv("FEISHU_WEBHOOK_URL")

if not DEEPSEEK_API_KEY:
    raise ValueError("没有读取到 DEEPSEEK_API_KEY")
if not TAVILY_API_KEY:
    raise ValueError("没有读取到 TAVILY_API_KEY")
if not FEISHU_WEBHOOK_URL:
    raise ValueError("没有读取到 FEISHU_WEBHOOK_URL")

MAX_BRIEF_ITEMS = 3
MAX_EVENT_RESEARCH_RESULTS = 5
MODEL_JSON_RETRIES = 1
SIGNAL_REPORT_FILE = Path("data/signal_probe/latest.json")
LAST_BRIEF_FILE = Path("data/last_brief.txt")
RUNS_DIR = Path("runs")
RUN_CONTEXT = None

DEEPSEEK = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com",
)
TAVILY = TavilyClient(api_key=TAVILY_API_KEY)


def start_run(dry_run=False):
    """为本次运行创建一个简单的日志目录。"""

    global RUN_CONTEXT
    started_at = datetime.now().astimezone()
    run_id = started_at.strftime("%Y-%m-%d_%H%M%S_%f")
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    RUN_CONTEXT = {
        "run_id": run_id,
        "run_dir": run_dir,
        "started_at": started_at,
        "dry_run": dry_run,
        "model_calls": [],
        "stage_counts": {},
    }
    write_run_json(
        "run_summary.json",
        {
            "run_id": run_id,
            "started_at": started_at.isoformat(),
            "dry_run": dry_run,
            "status": "running",
        },
    )
    return run_dir


def write_run_json(name, data):
    if not RUN_CONTEXT:
        return
    path = RUN_CONTEXT["run_dir"] / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def write_run_text(name, content):
    if not RUN_CONTEXT:
        return
    path = RUN_CONTEXT["run_dir"] / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content or "", encoding="utf-8")


def next_stage_key(stage):
    if not RUN_CONTEXT:
        return stage
    counts = RUN_CONTEXT["stage_counts"]
    counts[stage] = counts.get(stage, 0) + 1
    return f"{stage}_{counts[stage]:02d}"


def call_json_model(stage, messages, max_tokens, retries=MODEL_JSON_RETRIES):
    """调用 DeepSeek，并保证返回 JSON 对象。"""

    stage_key = next_stage_key(stage)
    last_error = None

    for attempt in range(retries + 1):
        request_messages = list(messages)
        if attempt:
            request_messages.append(
                {
                    "role": "user",
                    "content": (
                        "上一次响应不是合法 JSON。请重新执行原任务，"
                        "只输出符合原 schema 的严格 JSON。"
                    ),
                }
            )

        try:
            response = DEEPSEEK.chat.completions.create(
                model="deepseek-flash",
                messages=request_messages,
                response_format={"type": "json_object"},
                max_tokens=max_tokens,
                extra_body={"thinking": {"type": "disabled"}},
            )
            content = response.choices[0].message.content or ""
            write_run_text(
                f"model_responses/{stage_key}_attempt_{attempt + 1}.txt",
                content,
            )
            result = json.loads(content)
            if not isinstance(result, dict):
                raise ValueError("模型 JSON 顶层不是对象")
            if RUN_CONTEXT:
                RUN_CONTEXT["model_calls"].append(
                    {
                        "stage": stage_key,
                        "attempt": attempt + 1,
                        "status": "success",
                    }
                )
            return result
        except Exception as error:
            last_error = error
            if RUN_CONTEXT:
                RUN_CONTEXT["model_calls"].append(
                    {
                        "stage": stage_key,
                        "attempt": attempt + 1,
                        "status": "failed",
                        "error": str(error),
                    }
                )
            if attempt < retries:
                print(f"模型阶段 {stage} 失败，自动重试：{error}")

    raise ValueError(f"模型阶段 {stage} 失败：{last_error}")


def finish_run(status, error=None, counts=None):
    if not RUN_CONTEXT:
        return
    write_run_json("model_calls.json", RUN_CONTEXT["model_calls"])
    write_run_json(
        "run_summary.json",
        {
            "run_id": RUN_CONTEXT["run_id"],
            "started_at": RUN_CONTEXT["started_at"].isoformat(),
            "finished_at": datetime.now().astimezone().isoformat(),
            "dry_run": RUN_CONTEXT["dry_run"],
            "status": status,
            "error": str(error) if error else None,
            "counts": counts or {},
        },
    )


def clean_url(url):
    """去掉常见追踪参数，便于来源去重。"""

    if not isinstance(url, str) or not url.strip():
        return ""
    parts = urlsplit(url.strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
        and key.lower() not in {"ref", "source", "campaign"}
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), "")
    )


def load_signal_events(path=SIGNAL_REPORT_FILE):
    """读取信号验证器生成的过去 24 小时聚合事件。"""

    if not path.exists():
        raise FileNotFoundError(
            f"未找到信号候选文件：{path}，请先运行信号源验证器。"
        )

    report = json.loads(path.read_text(encoding="utf-8"))
    events = report.get("events", [])
    if not isinstance(events, list):
        raise ValueError("信号候选文件中的 events 格式不正确")

    write_run_json("signal_report.json", report)
    print(f"读取过去 24 小时聚合事件：{len(events)} 个。")
    return events


def select_brief_events(events):
    """让模型直接从聚合事件中选择今天值得查询的 1 至 3 个。"""

    if not events:
        return []

    overview = [
        {
            "index": index,
            "event": event.get("event_key", ""),
            "entity": event.get("entity", ""),
            "category": event.get("category", ""),
            "platforms": event.get("platforms", []),
            "platform_count": event.get("platform_count", 0),
            "best_rank": event.get("best_rank"),
            "signals": [
                {
                    "title": signal.get("title", ""),
                    "platform": signal.get("platform", ""),
                    "rank": signal.get("rank"),
                    "description": signal.get("description", "")[:300],
                }
                for signal in event.get("signals", [])
            ],
        }
        for index, event in enumerate(events)
    ]

    prompt = f"""
你是 AI Frontier 的日报编辑。请从过去 24 小时的聚合事件中，直接选择今天最值得进入简报调查的 1 至 {MAX_BRIEF_ITEMS} 个事件。

选择标准：
- 对 AI 能力、产品使用、开发生态、产业方向或安全风险有明确影响；
- 是具体事件，不是泛泛讨论或教程；
- 当前信号足以支持继续查询；
- 跨平台出现或榜单排名靠前时优先；
- 不要打分，不要输出观察名单，不要为了凑数降低标准。

为每个入选事件生成一个用于查询具体事件和来源的搜索 query。只输出严格 JSON：
{{
  "selected": [
    {{
      "index": 0,
      "query": "查询词",
      "reason": "入选原因"
    }}
  ]
}}

候选事件：
{json.dumps(overview, ensure_ascii=False, indent=2)}
"""

    result = call_json_model(
        "brief_event_selection",
        [{"role": "user", "content": prompt}],
        max_tokens=1200,
    )
    selected = []
    seen = set()

    for choice in result.get("selected", []):
        if not isinstance(choice, dict):
            continue
        index = choice.get("index")
        if not isinstance(index, int) or not 0 <= index < len(events):
            continue
        if index in seen:
            continue
        query = choice.get("query", "")
        if not isinstance(query, str) or not query.strip():
            query = events[index].get("event_key", "")
        selected.append(
            {
                "index": index,
                "query": query.strip(),
                "reason": str(choice.get("reason", "")).strip(),
                "event": events[index],
            }
        )
        seen.add(index)
        if len(selected) >= MAX_BRIEF_ITEMS:
            break

    write_run_json("selected_events.json", selected)
    print(f"模型选出 {len(selected)} 个事件进入查询。")
    return selected


def research_selected_events(selected):
    """查询入选事件，同时保留发现信号中的来源。"""

    packages = []
    for choice in selected:
        event = choice["event"]
        print(f"查询事件：{event.get('event_key', '')}")
        sources = []
        seen_urls = set()

        for signal in event.get("signals", []):
            url = clean_url(signal.get("url", ""))
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            sources.append(
                {
                    "title": signal.get("title", ""),
                    "url": url,
                    "date": signal.get("published_at"),
                    "content": signal.get("description", ""),
                    "source_type": "discovery_signal",
                }
            )

        try:
            response = TAVILY.search(
                query=choice["query"],
                topic="general",
                search_depth="advanced",
                max_results=MAX_EVENT_RESEARCH_RESULTS,
                include_answer=False,
                include_raw_content=True,
                include_published_date=True,
                safe_search=True,
                time_range="day",
            )
        except Exception as error:
            print(f"事件查询失败，保留发现来源：{error}")
            response = {"results": []}

        for result in response.get("results", []):
            url = clean_url(result.get("url", ""))
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            sources.append(
                {
                    "title": result.get("title", ""),
                    "url": url,
                    "date": result.get("published_date"),
                    "content": (
                        result.get("raw_content")
                        or result.get("content", "")
                    )[:6000],
                    "source_type": "event_search",
                }
            )

        if sources:
            packages.append(
                {
                    "event": event,
                    "selection_reason": choice["reason"],
                    "query": choice["query"],
                    "sources": sources,
                }
            )

    write_run_json("event_research.json", packages)
    print(f"完成 {len(packages)} 个事件的来源查询。")
    return packages


def compact_headline(title, max_chars=40):
    cleaned = " ".join(str(title or "AI 更新").split()).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 1].rstrip() + "…"


def write_brief_items(packages):
    """根据查询材料直接生成日报正文。"""

    if not packages:
        return []

    prompt = f"""
你是 AI Frontier 的中文资讯编辑。请根据下面已经选中的事件和查询来源，直接生成今天的日报条目。

规则：
1. 每个事件输出一条，最多 {MAX_BRIEF_ITEMS} 条。
2. title 控制在 18 至 40 个中文字符；body 用 2 至 4 句话，约 100 至 200 个中文字符。
3. 只使用 sources 中能找到的信息，不补充外部事实。
4. 如果来源说法不同，自然说明“目前信息显示”或“据相关报道”。
5. source_ids 选择 1 至 3 个最有用的来源，优先原始来源和直接报道。
6. 不输出事实核验、重要度评分、观察名单或内部审核术语。
7. 查询后仍没有具体信息的事件可以不输出。

只输出严格 JSON：
{{
  "items": [
    {{
      "event_index": 0,
      "title": "日报标题",
      "body": "日报正文",
      "source_ids": [0, 1]
    }}
  ]
}}

事件与来源：
{json.dumps(packages, ensure_ascii=False, indent=2)}
"""

    result = call_json_model(
        "brief_writing",
        [{"role": "user", "content": prompt}],
        max_tokens=2600,
    )
    items = []

    for raw_item in result.get("items", []):
        if not isinstance(raw_item, dict):
            continue
        event_index = raw_item.get("event_index")
        if not isinstance(event_index, int) or not 0 <= event_index < len(packages):
            continue
        title = str(raw_item.get("title", "")).strip()
        body = str(raw_item.get("body", "")).strip()
        if not title or not body:
            continue

        package = packages[event_index]
        source_ids = []
        for source_id in raw_item.get("source_ids", []):
            if not isinstance(source_id, int):
                continue
            if not 0 <= source_id < len(package["sources"]):
                continue
            if source_id not in source_ids:
                source_ids.append(source_id)
            if len(source_ids) >= 3:
                break
        if not source_ids:
            source_ids = list(range(min(2, len(package["sources"]))))

        items.append(
            {
                "title": compact_headline(title),
                "body": body,
                "sources": [package["sources"][index] for index in source_ids],
            }
        )
        if len(items) >= MAX_BRIEF_ITEMS:
            break

    write_run_json("brief_items.json", items)
    print(f"生成日报条目：{len(items)} 条。")
    return items


def build_signal_brief():
    events = load_signal_events()
    selected = select_brief_events(events)
    packages = research_selected_events(selected)
    return write_brief_items(packages)


def append_sources(lines, sources):
    urls = []
    for source in sources:
        url = source.get("url", "")
        if url and url not in urls:
            urls.append(url)
        if len(urls) >= 3:
            break
    if urls:
        lines.append("来源：" + " ｜ ".join(urls))


def build_message(items):
    lines = ["AI Frontier｜每日 AI 简报", ""]

    if not items:
        lines.extend(
            ["今天没有从过去 24 小时的候选中选出适合进入简报的事件。", ""]
        )
    else:
        for index, item in enumerate(items, start=1):
            lines.append(f"{index}. {item['title']}")
            lines.append("")
            lines.append(item["body"])
            append_sources(lines, item.get("sources", []))
            lines.append("")

    lines.append("由 AI Frontier 自动发现、查询与整理。")
    return "\n".join(lines)


def save_last_brief(message):
    LAST_BRIEF_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAST_BRIEF_FILE.write_text(message, encoding="utf-8")


def send_to_feishu(message):
    response = requests.post(
        FEISHU_WEBHOOK_URL,
        json={"msg_type": "text", "content": {"text": message}},
        timeout=15,
    )
    response.raise_for_status()
    result = response.json()
    if result.get("code") != 0 and result.get("StatusCode") != 0:
        raise ValueError(f"飞书推送失败：{result}")


def main(dry_run=False):
    start_run(dry_run=dry_run)
    print("AI Frontier 开始运行。\n")

    try:
        items = build_signal_brief()
        message = build_message(items)
        save_last_brief(message)
        write_run_text("brief.txt", message)

        if dry_run:
            print("===== AI Frontier 预览 =====\n")
            print(message)
            print("\ndry-run 完成，未推送飞书。")
        else:
            send_to_feishu(message)
            print("飞书日报推送成功。")

        finish_run(
            "success",
            counts={"selected_brief_items": len(items)},
        )
    except Exception as error:
        finish_run("failed", error=error)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate the AI Frontier daily brief."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="generate and preview without delivery",
    )
    args = parser.parse_args()

    try:
        main(dry_run=args.dry_run)
    except Exception as error:
        print("\nAI Frontier 运行失败：")
        print(error)
        raise SystemExit(1)
