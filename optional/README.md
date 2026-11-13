# 可选机器兜底（只适用于 Claude 桌面端）

核心规则不依赖本目录：不装也能完整使用。本目录把“靠自觉”的几条做成脚本，装了更稳。**只适用于 Claude 桌面端**（用到它的 PreToolUse 钩子、定时任务和会话工具）；别的环境不要装。

| 文件 | 作用 | 状态 |
|---|---|---|
| `dispatch_guard.py` + `派单定档.json` | 派窗口、派子代理前核对“执行档”声明和定档表；本机 `start_session` 缺 `permission_mode` 也拦；每次拦截追加一行到 `~/.claude/collab/dispatch_guard_blocks.log` | 可选 |
| `add_global_dispatch_hook.py` | 把派单钩子追加进设置文件（先 `--dry-run`） | 可选 |
| `collab_guard.py` + `../templates/collab_guard.json` + `../templates/lead-desk-settings.json` | 总控台写入守卫：总控只能写白名单路径；先只记录，到期再切真拦 | 可选 |
| `add_gate_ask.py` | 给 6 条门禁命令（`git push`、`git merge`、`gh pr merge`、`gh repo create`、`gh repo edit`、`gh release create`）加确认弹框；弹框不算授权 | 可选 |
| `patrol.py` + `patrol-task.md` | 巡检脚本和定时任务提示词（每天检查、每周建议） | **试点、可选**，部分能力未实测，见 `patrol-task.md` 文末 |

## 安装顺序（派单钩子；守卫同理）

脚本缺失时，钩子命令退出码为 2 = 拦截，所有会话的派单会被拦死，所以顺序不能倒：

1. 把 `dispatch_guard.py`、`派单定档.json` 放进 `~/.claude/collab/`（路径里的 `<HOME>` 换成你的用户目录，用正斜杠）。
2. 验证退出码为 0：`echo {} | python -X utf8 -I ~/.claude/collab/dispatch_guard.py; echo $?`。
3. 再装钩子：`python -I add_global_dispatch_hook.py --dry-run` 看结果，确认后去掉 `--dry-run`。
4. 撤脚本、改名或升级前，**先撤钩子**。

总控台守卫同理：先放 `collab_guard.py`，验空输入退出码为 0，把模板里的 `<HOME>` 换成绝对路径并确认没有残留，再装 `lead-desk-settings.json`；配完做一次越界测试。

## 必须改的地方

- `派单定档.json` 里的 `allow` 表把“贵／中／便宜”绑定到具体型号族（`opus`／`sonnet`／`haiku`）；换型号只改表，不改规则正文。
- `dispatch_guard.py` 的 `MARKERS` 只认项目根的 `状态.md`；状态文件另有名字的，把文件名加进去。
- 这些脚本只读钩子输入和自己的配置，不读、不打印设置文件内容；`add_*.py` 改设置文件前会按字节备份到 `~/.claude/备份/`，备份按密钥文件对待，不要读它、不要提交它。
- 设置文件的改动是安全设置，请由你本人确认后再执行。
