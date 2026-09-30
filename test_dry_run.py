import daily_brief


calls = {"send": 0}
originals = {
    "build_signal_brief": daily_brief.build_signal_brief,
    "save_last_brief": daily_brief.save_last_brief,
    "send_to_feishu": daily_brief.send_to_feishu,
    "start_run": daily_brief.start_run,
    "finish_run": daily_brief.finish_run,
    "write_run_text": daily_brief.write_run_text,
}

try:
    daily_brief.build_signal_brief = lambda: [
        {
            "title": "测试主题",
            "body": "测试正文",
            "sources": [],
        }
    ]
    daily_brief.save_last_brief = lambda message: None
    daily_brief.send_to_feishu = lambda message: calls.__setitem__(
        "send", calls["send"] + 1
    )
    daily_brief.start_run = lambda dry_run=False: None
    daily_brief.finish_run = lambda *args, **kwargs: None
    daily_brief.write_run_text = lambda *args, **kwargs: None

    daily_brief.main(dry_run=True)
finally:
    for name, value in originals.items():
        setattr(daily_brief, name, value)

assert calls["send"] == 0
print("精简主流程 dry-run 不推送飞书测试通过。")
