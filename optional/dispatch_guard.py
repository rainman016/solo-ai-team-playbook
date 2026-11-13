"""派单守卫（PreToolUse 钩子，全局）：派窗口和派子代理前，核对“执行档”声明、参数和定档表。

只在协作项目里生效：钩子输入的 cwd 或其任一祖先目录里有 状态.md，且该文件前 20 行含“总控会话”或 会话编号；否则一律放行。
只处理 Agent（subagent_type 为 fork 放行）和 mcp__ccd_session__start_session
（只有 proposal_id 即启动已提议的卡片、或 initiation 为 offer 且没有 prompt 的放行）；其他工具一律放行、什么也不做。

prompt 里必须有一行（原样格式）：
    执行档：<角色>·<模型>·<强度>      模型 opus／sonnet／haiku，强度 low／medium／high
用比定档表更贵的档时，另加一行：
    升档：<理由>                       理由必须以“高风险／返修满2轮／协商2轮分歧／改线上规则”之一开头（只允许表里 upgrade_allow 的档）；
                                      或以“owner批准”开头、后面再写至少 6 个字符（时间＋owner 的原话），放行任意已知模型（含 fable）和强度。
                                      平时声明 fable 不在任何角色允许范围，只能经“owner批准”放行。

拒绝条件：缺声明行（拒绝原因里列出角色表并给一行示例）；角色不在表里；调用参数 model／effort 与声明不一致；
声明的档超出该角色允许范围且没有合格的升档行；start_session 没传 model、effort 或 permission_mode
（只带 proposal_id 的启动卡片调用放行；带 proposal_id 和说明补发的只查“执行档”行；context 为 fork 的放行；
target 为 cloud／ssh 的不查 permission_mode）；Agent／Task 没传 model（不查 permission_mode）。Codex 派单走 Bash，不检查。
拒绝时输出 PreToolUse deny 和一句中文原因。

表：默认 ~/.claude/collab/派单定档.json（--table <路径> 可换，供测试）。
拦截日志：每拦一次追加一行（制表符分隔：北京时间、项目、工具名、角色·档、原因前 60 字）到
  ~/.claude/collab/dispatch_guard_blocks.log（环境变量 DISPATCH_GUARD_BLOCK_LOG 可换，供测试）；放行不写；写失败不影响拦截。
自身出错（读不到表、解析失败等）：放行，只把错误类型写进 ~/.claude/collab/dispatch_guard_errors.log
  （环境变量 DISPATCH_GUARD_ERR_LOG 可换，供测试）。出在读钩子输入或读定档表时，错误类型后带“输入”或“定档表”。
“总控会话：无”（单窗口项目）不算协作标记。
只读钩子输入和那张表，不读、不打印任何设置文件。
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SS = "mcp__ccd_session__start_session"
AGENT_TOOLS = {"Agent", "Task"}
DECL_RE = re.compile(r"^[ \t>*\-]*执行档[:：][ \t]*(.*?)[ \t]*$", re.M)
UP_RE = re.compile(r"^[ \t>*\-]*升档[:：][ \t]*(.*?)[ \t]*$", re.M)
PARTS_RE = re.compile(r"^(\S+?)\s*[·・]\s*(opus|sonnet|haiku|fable)\s*[·・]\s*(low|medium|high)$", re.I)
FAMILIES = ("opus", "sonnet", "haiku", "fable")
EFFORTS = ("low", "medium", "high")
APPROVE_WORD, APPROVE_MIN = "owner批准", 6  # 表里 owner_approval_word／owner_approval_min_chars 可覆盖
# 协作项目标记：cwd 的任一祖先目录里有这些文件之一，且前 20 行含“总控会话”或 会话编号
MARKERS = ("状态.md",)  # 状态文件不叫这个名字的项目，把文件名加进来
TAG_RE = re.compile(r"总控会话(?![*`]*[ \t]*[:：][ \t*`（(]*无)|local[_][0-9a-fA-F]")  # “总控会话：无”＝单窗口，不算协作标记
STAGE = ""  # 出错位置：读钩子输入时“输入”，读定档表时“定档表”，其余为空（写进错误日志）


def collab_root(cwd: Path):
    """命中的协作项目目录（状态文件所在目录），没命中返回 None。"""
    for d in [cwd, *cwd.parents]:
        for rel in MARKERS:
            f = d / rel
            try:
                if f.is_file():
                    with open(f, encoding="utf-8", errors="replace") as fh:
                        head = "".join(next(fh, "") for _ in range(20))
                    if TAG_RE.search(head):
                        return d
            except OSError:
                continue
    return None



def family(model: str) -> str:
    m = model.lower()
    return next((f for f in FAMILIES if f in m), m)


def hint(roles: dict) -> str:
    """拒绝原因里附的角色表和一行示例。"""
    ex_role = "子代理核对" if "子代理核对" in roles else next(iter(roles), "<角色>")
    return f"角色只能是：{'、'.join(roles)}；示例：执行档：{ex_role}·haiku·low"


def approved(up_text: str, table: dict) -> bool:
    """升档行以“owner批准”开头，且后面还有至少 N 个字符（时间＋原话）。"""
    word = str(table.get("owner_approval_word") or APPROVE_WORD)
    need = int(table.get("owner_approval_min_chars") or APPROVE_MIN)
    return up_text.startswith(word) and len(up_text[len(word):].strip(" \t：:，,、-—")) >= need


def check(tool: str, ti: dict, table: dict):
    """返回拒绝原因（一句中文），合规或免检返回 None。"""
    prompt = str(ti.get("prompt") or "")
    relaxed = False  # True：只查“执行档”行，不强制 model／effort／permission_mode
    skip_pm = False
    if tool == SS:
        if ti.get("proposal_id") and not prompt.strip():
            return None  # 启动已提议的卡片
        if ti.get("initiation") == "offer" and not prompt.strip():
            return None  # 只先弹“新会话／就在这做”卡片，还没到派单
        if str(ti.get("context") or "").lower() == "fork":
            return None  # fork 继承本窗口模型、强度和权限
        if ti.get("proposal_id"):
            relaxed = True  # 带说明补发的卡片：平台收不收这几个参数没实测过，不堵
        if str(ti.get("target") or "").lower() in ("cloud", "ssh"):
            skip_pm = True  # 云端／ssh 窗口没有本机权限模式
    elif ti.get("subagent_type") == "fork":
        return None  # fork 继承本窗口模型

    roles = table.get("roles") or {}
    how = "按《总控专用规则》“派单定档”，在 prompt 里写一行“执行档：<角色>·<模型>·<强度>”，并传 model" + ("、effort" + ("；开窗还要传 permission_mode" if not relaxed and not skip_pm else "") if tool == SS else "")
    m = DECL_RE.search(prompt)
    if not m:
        return f"派单被拦：prompt 里缺“执行档”声明行。{how}。{hint(roles)}。"
    pm = PARTS_RE.match(m.group(1))
    if not pm:
        return (f"派单被拦：“执行档”声明格式不对（当前：{m.group(0).strip()[:40]}）。"
                f"要写成“执行档：<角色>·<模型 opus/sonnet/haiku>·<强度 low/medium/high>”。{hint(roles)}。")
    role, d_model, d_effort = pm.group(1), pm.group(2).lower(), pm.group(3).lower()
    if role not in roles:
        return f"派单被拦：执行档里的角色“{role}”不在定档表里。{hint(roles)}。"

    p_model, p_effort = str(ti.get("model") or "").strip(), str(ti.get("effort") or "").strip()
    if not relaxed:
        if not p_model:
            return f"派单被拦：没传 model 参数。声明是 {d_model}，调用时要同时传 model={d_model}。"
        if tool == SS and not p_effort:
            return f"派单被拦：start_session 没传 effort 参数（不传会继承总控的强度）。声明是 {d_effort}，调用时要传 effort={d_effort}。"
    # 传了就必须和声明一致（补发卡片 relaxed 时也比，不传不拦）
    if p_model and family(p_model) != d_model:
        return f"派单被拦：model 参数（{p_model}）和声明的模型（{d_model}）不一致，请改成一致。"
    if p_effort and p_effort.lower() != d_effort:
        return f"派单被拦：effort 参数（{p_effort}）和声明的强度（{d_effort}）不一致，请改成一致。"
    if not relaxed:
        if tool == SS and not skip_pm and not str(ti.get("permission_mode") or "").strip():
            return ("派单被拦：start_session 没传 permission_mode 参数（不传的话，新窗口每条命令都会弹框问 owner）。"
                    "请补上 permission_mode，填成和总控当前一样的权限档；平台不让传比总控更高的档。")

    pair = [d_model, d_effort]
    if pair in [list(x) for x in roles[role].get("allow", [])]:
        return None
    up = UP_RE.search(prompt)
    up_text = up.group(1) if up else ""
    reasons = table.get("upgrade_reasons") or []
    word = str(table.get("owner_approval_word") or APPROVE_WORD)
    if up and approved(up_text, table):
        known_m = [str(x).lower() for x in table.get("known_models") or FAMILIES]
        known_e = [str(x).lower() for x in table.get("known_efforts") or EFFORTS]
        if d_model in known_m and d_effort in known_e:
            return None
    if up and any(up_text.startswith(r) for r in reasons) and pair in [list(x) for x in table.get("upgrade_allow", [])]:
        return None
    allowed = "、".join(f"{a}·{b}" for a, b in roles[role].get("allow", []))
    base = (roles[role].get("allow") or [[None, None]])[0]
    mr, er = ["haiku", "sonnet", "opus", "fable"], ["low", "medium", "high"]
    if (base[0] in mr and base[1] in er and d_model in mr and d_effort in er
            and mr.index(d_model) <= mr.index(base[0]) and er.index(d_effort) <= er.index(base[1])):
        return f"派单被拦：{role}的档位 {d_model}·{d_effort} 超出定档表，该角色只能用：{allowed}。"
    ask = f"理由以这四个词之一开头：{'、'.join(reasons)}；或 owner 批准的，写“升档：{word}＋时间＋owner 的原话”（仅限项目总控凭 owner 本人原话写）"
    if up and up_text.startswith(word):
        return f"派单被拦：“{word}”后面要写时间和 owner 的原话（至少 6 个字符），才能放行；仅限项目总控凭 owner 本人原话写。"
    if up and not any(up_text.startswith(r) for r in reasons):
        return f"派单被拦：“升档”理由不合格，{ask}。"
    return (f"派单被拦：{role}的档位 {d_model}·{d_effort} 超出定档表（允许：{allowed}）。"
            f"确需更贵的档，另加一行“升档：<理由>”，{ask}。")


def log_path(env_name: str, default_name: str) -> Path:
    return Path(os.environ.get(env_name) or (Path.home() / ".claude" / "collab" / default_name))


def log_block(project: str, tool: str, ti: dict, reason: str) -> None:
    """拦一次追加一行：北京时间、项目（状态文件所在目录名）、工具名、角色或执行档、原因前 60 字（制表符分隔）。写不进去不影响拦截。"""
    try:
        m = DECL_RE.search(str(ti.get("prompt") or ""))
        who = re.sub(r"\s+", "", m.group(1)) if m else "无声明"
        ts = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        line = "\t".join([ts, project, tool, who[:40], re.sub(r"\s+", " ", reason)[:60]])
        p = log_path("DISPATCH_GUARD_BLOCK_LOG", "dispatch_guard_blocks.log")
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def main() -> int:
    argv = sys.argv[1:]
    table_path = Path(argv[argv.index("--table") + 1]) if "--table" in argv else Path.home() / ".claude" / "collab" / "派单定档.json"
    global STAGE
    STAGE = "输入"
    data = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    STAGE = ""
    tool = data.get("tool_name", "")
    if tool != SS and tool not in AGENT_TOOLS:
        return 0
    root = collab_root(Path(data.get("cwd") or Path.cwd()).resolve())
    if root is None:
        return 0  # 不是协作项目（不在任何带“总控会话”标记的项目里）：一律放行，也不读表
    STAGE = "定档表"
    table = json.loads(table_path.read_text(encoding="utf-8"))
    STAGE = ""
    reason = check(tool, data.get("tool_input") or {}, table)
    if reason:
        log_block(root.name, tool, data.get("tool_input") or {}, reason)
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except Exception as e:  # 自身出错一律放行，只记错误类型
        try:
            err = log_path("DISPATCH_GUARD_ERR_LOG", "dispatch_guard_errors.log")
            err.parent.mkdir(parents=True, exist_ok=True)
            with open(err, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {type(e).__name__}{' ' + STAGE if STAGE else ''}\n")
        except Exception:
            pass
        sys.exit(0)
