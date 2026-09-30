# AI Frontier

AI Frontier 是一个轻量的 AI 热点日报 Agent。它持续读取国内外公开信号源，将过去 24 小时的候选聚合成事件，再由模型选出 1–3 个最值得关注的事件，查询具体来源并生成飞书日报。

## 工作流程

```text
公开信号源
  → 过去 24 小时 AI 候选
  → 轻量事件聚合
  → 模型选择 1–3 个事件
  → 查询具体事件与来源
  → 生成中文日报
  → 飞书 Webhook 推送
```

## 主要能力

- **多源发现**：知乎、抖音、B 站、少数派、掘金、HelloGitHub、Hacker News、Hugging Face、GitHub Trending、Product Hunt、Google Trends。
- **轻量聚合**：按事件主体和类型合并明确重复的跨平台信号。
- **模型选题**：每天只选择 1–3 个值得进入简报的事件，不为凑数降低标准。
- **来源追踪**：保留发现链接，并通过 Tavily 查询具体事件与补充来源。
- **自动推送**：生成中文日报，通过飞书自定义机器人 Webhook 推送。
- **可观测运行**：每次运行的候选、模型响应、查询材料和最终简报都会保存到本地 `runs/`。

## 项目结构

```text
.
├── daily_brief.py                 # 选题、事件查询、日报生成、飞书推送
├── signal_source_probe.py         # 信号抓取、AI 筛选、事件聚合
├── signal_aggregator/             # DailyHot 本地聚合服务
├── scripts/
│   ├── run_signal_probe.sh        # 刷新信号候选
│   ├── run_daily_brief.sh         # 完整日报流程
│   └── install_daily_push.py      # macOS LaunchAgent 定时任务
├── test_signal_source_probe.py    # 信号源与事件聚合测试
├── test_signal_brief_flow.py      # 选题、查询、写作测试
└── test_dry_run.py                # dry-run 不推送测试
```

## 环境要求

- Python 3.9+
- Node.js 18+
- npm
- macOS（仅定时任务安装脚本依赖 macOS；手动运行不受此限制）

## 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/<your-account>/ai-frontier-agent.git
cd ai-frontier-agent
```

### 2. 安装 Python 依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Node 依赖会在第一次运行信号验证器时自动安装；也可以提前安装：

```bash
cd signal_aggregator
npm install
cd ..
```

### 3. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`：

```dotenv
DEEPSEEK_API_KEY=your_deepseek_api_key
TAVILY_API_KEY=your_tavily_api_key
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/your_webhook_token
```

`.env` 已加入 `.gitignore`，不要提交真实密钥或 Webhook。

### 4. 运行测试

```bash
.venv/bin/python test_signal_source_probe.py
.venv/bin/python test_signal_brief_flow.py
.venv/bin/python test_dry_run.py
```

### 5. 预览日报

```bash
AI_FRONTIER_DRY_RUN=1 scripts/run_daily_brief.sh
```

该命令会刷新信号源、聚合事件、完成选题与来源查询，并在终端预览日报，但不会推送飞书。

### 6. 正式推送

```bash
scripts/run_daily_brief.sh
```

## macOS 每日定时推送

安装或更新每天 09:30 的 LaunchAgent：

```bash
.venv/bin/python scripts/install_daily_push.py install --time 09:30
```

查看状态：

```bash
.venv/bin/python scripts/install_daily_push.py status
```

卸载：

```bash
.venv/bin/python scripts/install_daily_push.py uninstall
```

定时任务依赖电脑处于可执行状态、当前用户会话可用、网络正常以及 API Key 有效。

## 输出位置

- `data/signal_probe/latest.json`：最近一次信号与事件聚合结果
- `data/last_brief.txt`：最近一次日报正文
- `runs/<run-id>/`：单次运行的候选、模型响应、查询材料和结果
- `logs/`：定时任务与聚合服务日志

这些运行数据已加入 `.gitignore`，不会上传到 GitHub。

## 安全说明

公开仓库前请确认：

- `.env` 没有被 Git 跟踪；
- Git 历史中没有真实 API Key、Webhook 或 Cookie；
- `data/`、`runs/`、`logs/` 中没有需要公开的运行数据；
- 飞书自定义机器人已设置必要的关键词或安全策略。

## 当前边界

- GitHub Trending 依赖公开页面结构，页面改版后可能需要调整解析器。
- 国内信号源依赖 DailyHot 上游接口，个别平台可能受反爬或访问限制影响。
- 事件聚合采用简单规则，只处理明确的同主体、同类型重复事件。
- 日报内容由模型根据公开来源生成，使用者仍应自行判断其适用场景。

## Roadmap

- 增加 Linux cron / systemd 定时运行说明
- 增加来源健康状态摘要
- 改进旧模型重新升温的识别方式
- 增加可选的邮件、Slack 或 Telegram 输出
