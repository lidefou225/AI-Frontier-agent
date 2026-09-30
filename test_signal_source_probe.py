from datetime import datetime, timedelta, timezone

import signal_source_probe as probe


captured_at = datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc)
raw = {
    "id": "abc",
    "title": "某个人 AI Agent 下载量继续增长",
    "desc": "个人 AI 代理进入应用榜单前列",
    "hot": 123456,
    "url": "https://example.com/muse",
    "timestamp": int((captured_at - timedelta(hours=2)).timestamp() * 1000),
}
signal = probe.normalize_signal("testplatform", 3, raw, captured_at)
assert signal["signal_id"] == "testplatform:abc"
assert signal["rank"] == 3
assert signal["heat"] == 123456
assert probe.within_lookback(signal, captured_at, 24)
assert "weibo" not in probe.DEFAULT_DAILYHOT_SOURCES

old_signal = dict(signal)
old_signal["published_at"] = (
    captured_at - timedelta(hours=30)
).isoformat()
assert not probe.within_lookback(old_signal, captured_at, 24)

noise = probe.normalize_signal(
    "douyin",
    1,
    {
        "id": "noise",
        "title": "今晚足球比赛结果",
        "url": "https://example.com/football",
    },
    captured_at,
)
candidates = probe.keyword_candidates([signal, noise])
assert [row["signal_id"] for row in candidates] == ["testplatform:abc"]

cross_platform = {
    **signal,
    "signal_id": "hackernews:42",
    "platform": "hackernews",
    "rank": 8,
    "event_key": "个人 AI Agent 下载与采用增长",
    "entity": "个人 AI Agent",
    "category": "agent_adoption",
}
classified = [
    {
        **signal,
        "event_key": "个人 AI Agent 下载与采用增长",
        "entity": "个人 AI Agent",
        "category": "agent_adoption",
    },
    cross_platform,
]
events = probe.group_events(classified)
assert len(events) == 1
assert events[0]["platform_count"] == 2
assert events[0]["best_rank"] == 3
assert events[0]["signal_count"] == 2


class FakeResponse:
    def __init__(self, json_data=None, text="", content=b""):
        self._json_data = json_data
        self.text = text
        self.content = content or text.encode("utf-8")

    def raise_for_status(self):
        return None

    def json(self):
        return self._json_data


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.last_url = None
        self.last_params = None

    def get(self, url, timeout, params=None):
        self.last_url = url
        self.last_params = params
        assert timeout == probe.REQUEST_TIMEOUT
        return self.response


dailyhot_payload = {
    "code": 200,
    "updateTime": captured_at.isoformat(),
    "data": [raw],
}
rows, status = probe.fetch_dailyhot_source(
    FakeSession(FakeResponse(json_data=dailyhot_payload)),
    "http://127.0.0.1:3210",
    "zhihu",
    10,
    captured_at,
)
assert len(rows) == 1
assert status["status"] == "ok"
assert status["source"] == "zhihu"

hf_payload = [
    {
        "modelId": "org/agent-model",
        "tags": ["transformers", "agent"],
        "trendingScore": 88,
        "likes": 120,
        "downloads": 3400,
        "createdAt": "2026-09-29T10:00:00Z",
    }
]
hf_session = FakeSession(FakeResponse(json_data=hf_payload))
rows, status = probe.fetch_hugging_face(hf_session, 10, captured_at)
assert rows[0]["platform"] == "huggingface"
assert rows[0]["metrics"]["downloads"] == 3400
assert hf_session.last_params["sort"] == "trendingScore"

github_html = """
<article class="Box-row">
  <h2><a href="/org/agent-repo"> org / agent-repo </a></h2>
  <p>An open source AI agent framework.</p>
  <span class="float-sm-right">1,234 stars today</span>
</article>
"""
rows, status = probe.fetch_github_trending(
    FakeSession(FakeResponse(text=github_html)),
    10,
    captured_at,
)
assert rows[0]["title"] == "org/agent-repo"
assert rows[0]["heat"] == 1234

product_hunt_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>ph-1</id>
    <title>Agent Desk</title>
    <published>2026-09-29T12:00:00Z</published>
    <content type="html">&lt;p&gt;AI agent workspace&lt;/p&gt;</content>
    <link rel="alternate" href="https://www.producthunt.com/posts/agent-desk" />
  </entry>
</feed>
"""
rows, status = probe.fetch_product_hunt(
    FakeSession(FakeResponse(content=product_hunt_xml)),
    10,
    captured_at,
)
assert rows[0]["platform"] == "producthunt"
assert rows[0]["description"] == "AI agent workspace"

google_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0">
  <channel><item>
    <title>new ai model</title>
    <link>https://trends.google.com/trending/rss?geo=US</link>
    <pubDate>Tue, 29 Sep 2026 12:00:00 +0000</pubDate>
    <ht:approx_traffic>20,000+</ht:approx_traffic>
    <ht:news_item><ht:news_item_title>New AI model launches</ht:news_item_title></ht:news_item>
  </item></channel>
</rss>
"""
rows, status = probe.fetch_google_trends(
    FakeSession(FakeResponse(content=google_xml)),
    10,
    captured_at,
)
assert rows[0]["platform"] == "google_trends"
assert rows[0]["heat"] == 20000
assert rows[0]["metrics"]["geo"] == "US"

claude_signals = [
    {
        **signal,
        "signal_id": "google_trends:claude-auth",
        "platform": "google_trends",
        "rank": 1,
        "event_key": "Claude 无法认证服务中断",
        "entity": "Claude",
        "category": "ai_service_outage",
    },
    {
        **signal,
        "signal_id": "hackernews:claude-outage",
        "platform": "hackernews",
        "rank": 1,
        "event_key": "Claude 部分服务中断",
        "entity": "Claude",
        "category": "ai_service_outage",
    },
]
claude_events = probe.group_events(claude_signals)
assert len(claude_events) == 1
assert claude_events[0]["platform_count"] == 2
assert claude_events[0]["signal_count"] == 2
assert claude_events[0]["aggregation_basis"] == "same_entity_and_category"

generic_ai_signals = [
    {
        **signal,
        "signal_id": "source:a",
        "event_key": "AI 数据库客户端发布",
        "entity": "AI",
        "category": "ai_development",
    },
    {
        **signal,
        "signal_id": "source:b",
        "event_key": "AI 游戏生成教程",
        "entity": "AI",
        "category": "ai_development",
    },
]
assert len(probe.group_events(generic_ai_signals)) == 2

print("信号归一化、海外源解析、轻量事件聚合和跨平台合并测试通过。")
