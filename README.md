# Knowledge Archive Engine

可审计的本地 AI 知识库引擎。Agent 负责知识判断，程序负责来源转换、校验、媒体、持久化与发布。个人账号、分类、正文与媒体保留在独立知识库中。

## 当前能力

- 来源无关的 JSON 真源、分类、标签与综述；合成非 X 来源已通过端到端测试。
- X 普通推文、Note Tweet、图片、视频、文章和引用转换；保留来源关系与删除线等格式。
- 按稳定作者 ID 和回复边恢复连续推文，不把其他读者评论混入作者线程。
- 官方 Chrome DevTools CLI 后端，有界滚动和特定“更多回复”按钮展开；逐观察保存证据，可校验后恢复。
- 旧六文件 X 归档一次性迁移，原始文件不删除；显式保留历史采集缺口。
- 媒体哈希验证、下载主机白名单、不可变构建版本和原子发布指针。
- SQLite `entries`、`records`、`members` 三张表，以及 Markdown 阅读视图。

**尚未实现账户级来源列表的自动扫描与调度。** 单帖采集不是完整点赞/转发同步。也不承诺恢复 X 已删除、隐藏或无权查看的内容。`observed` 仅描述本次可见范围；`partial` 和预算耗尽必须作为缺口保留。

## 安装与离线演示

运行环境：macOS/Linux，Python 3.11+。Python 核心无第三方运行时依赖；浏览器采集另需 Node.js 与官方 `chrome-devtools-mcp`（已验证 1.12.0）。Windows 的单机文件锁与发布链接暂未支持。

```sh
uv venv --python 3.13
uv pip install .
cp -R examples/demo /tmp/knowledge-archive-demo
.venv/bin/knowledge-archive --root /tmp/knowledge-archive-demo check
.venv/bin/knowledge-archive --root /tmp/knowledge-archive-demo build
```

演示内容全部为合成样本，无需浏览器、账号或网络。阅读 `/tmp/knowledge-archive-demo/view/README.md`。

## 知识库操作

```sh
knowledge-archive --root /path/to/archive check
knowledge-archive --root /path/to/archive pending
knowledge-archive --root /path/to/archive label ENTRY_ID --l2 1.1 --ttl tactical --status active
knowledge-archive --root /path/to/archive synthesis 1.1 /path/to/bullets.json
knowledge-archive --root /path/to/archive audit
knowledge-archive --root /path/to/archive fetch
knowledge-archive --root /path/to/archive build
```

所有正式变更写进 `raw/archive.json`；证据位于 `raw/evidence/`，媒体位于 `media/`。构建先在 `.archive/builds/` 完整生成，再切换 `view`。失败保留上一版本。`media/` 需要独立备份，远程 URL 不是永久备份。

当前使用单机写锁，并检查源文件变化以避免覆盖人工编辑；不要让多台机器同时修改同一云盘目录。

## X 单帖与线程采集

```sh
chrome-devtools start --autoConnect --no-usage-statistics --no-performance-crux
python -m knowledge_archive.adapters.x.capture POST_ID \
  --output /path/to/private/capture --max-pages 12 --max-scrolls 6
# 中断或预算耗尽后恢复同一目录：
python -m knowledge_archive.adapters.x.capture POST_ID \
  --output /path/to/private/capture --resume
knowledge-archive --root /path/to/archive ingest-x /path/to/private/capture
```

可用 `--devtools-command node /path/to/chrome-devtools.js` 指定已安装的官方 CLI。探针不自动重启共享 daemon，只关闭自己新开的标签页。它不点赞、转发、关注，不读取或导出 Cookie。

每次观察原文直接落盘，Agent 只接收报告；不需要逐字转写 JSON。检查 `status`、`pending_page_ids`、`extraction_issues`、各访问的 `stop_reason`。只对明确的展开回复按钮进行点击，不点击泛化“更多”菜单。到达累计滚动上限需人工检查，不能无界重试。

## 迁移旧 X 库

先备份并停止旧写入任务。为实例创建 archive.toml（参考 examples/demo，X 媒体通常配置 pbs.twimg.com 与 video.twimg.com）。

```sh
knowledge-archive --root /path/to/old-archive migrate-x
knowledge-archive --root /path/to/old-archive check
knowledge-archive --root /path/to/old-archive build
```

迁移不覆盖已有新格式库。旧 JSON 保留作证据；确认新视图后，可将它们移到 raw/evidence/legacy，并更新实例维护契约。旧 tweets 表查询应改为 entries/records；文章文件名改用稳定记录身份。开发期可用本地包路径绑定实例，发布后固定版本。云盘可能给 `.pth` 标记隐藏属性，因此建议普通包安装而非 editable 安装。

## 测试与 Skill

```sh
PYTHONPATH=src python3 -B -m unittest discover -s tests -v
node --test tests/test_x_reader.cjs
```

[维护 Skill](skills/knowledge-archive/SKILL.md) 不含机器路径和账号，可安装到 Agent 的技能目录。[架构与数据边界](docs/architecture.md) 说明真源、证据和发布之间的关系。
