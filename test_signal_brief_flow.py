import daily_brief


events = [
    {
        "event_key": f"事件 {index}",
        "entity": f"实体 {index}",
        "category": "ai_product",
        "platforms": ["hackernews"],
        "platform_count": 1,
        "best_rank": index + 1,
        "aggregation_key": f"ai_product:entity:{index}",
        "signals": [
            {
                "title": f"发现信号 {index}",
                "platform": "hackernews",
                "rank": index + 1,
                "description": "事件描述",
                "url": f"https://example.com/discovery-{index}",
                "published_at": "2026-09-29T12:00:00+00:00",
            }
        ],
    }
    for index in range(4)
]


original_model = daily_brief.call_json_model
original_tavily = daily_brief.TAVILY


class FakeTavily:
    def search(self, **kwargs):
        return {
            "results": [
                {
                    "title": f"查询来源 {kwargs['query']}",
                    "url": "https://source.example.com/article",
                    "published_date": "2026-09-29",
                    "content": "具体事件信息",
                    "raw_content": "具体事件信息与来源内容",
                }
            ]
        }


def fake_model(stage, messages, max_tokens, retries=daily_brief.MODEL_JSON_RETRIES):
    if stage == "brief_event_selection":
        return {
            "selected": [
                {"index": 0, "query": "事件 0", "reason": "值得查询"},
                {"index": 1, "query": "事件 1", "reason": "值得查询"},
                {"index": 2, "query": "事件 2", "reason": "值得查询"},
                {"index": 3, "query": "事件 3", "reason": "不应超过上限"},
            ]
        }
    if stage == "brief_writing":
        return {
            "items": [
                {
                    "event_index": 0,
                    "title": "事件零进入今天的 AI 简报",
                    "body": "事件零出现了新的具体进展，查询结果提供了直接来源。",
                    "source_ids": [1],
                }
            ]
        }
    raise AssertionError(f"unexpected stage: {stage}")


try:
    daily_brief.call_json_model = fake_model
    daily_brief.TAVILY = FakeTavily()
    selected = daily_brief.select_brief_events(events)
    assert len(selected) == 3
    packages = daily_brief.research_selected_events(selected)
    assert len(packages) == 3
    assert len(packages[0]["sources"]) == 2
    items = daily_brief.write_brief_items(packages)
finally:
    daily_brief.call_json_model = original_model
    daily_brief.TAVILY = original_tavily

assert len(items) == 1
assert items[0]["title"] == "事件零进入今天的 AI 简报"
assert items[0]["sources"][0]["source_type"] == "event_search"
message = daily_brief.build_message(items)
assert "事件零进入今天的 AI 简报" in message
assert "https://source.example.com/article" in message
print("模型选取、事件查询、日报写作和消息生成测试通过。")
