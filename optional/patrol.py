"""外挂巡检·第一级（脚本，不调模型）。只读项目文件；正常时不输出，有异常每条输出一行。

用法：python -I patrol.py --root <项目根目录> [--status 状态.md] [--stale-hours 5]
       （--status 要写本项目状态文件的相对路径；项目状态文件不叫 状态.md 时必须指定，否则读不到状态文件，只报一行“读不到状态文件”，状态文件相关检查没跑）
       [--guard-config 总控台/.claude/collab_guard.json] [--probe 规则/__巡检探针__.md]
       [--observe-days 3] [--docs-only] [--limits <文档上限.json>] [--state-dir <去重状态目录>] [--tz-offset +8]

检查：
1. 任务池在途的线：最后动静（任务池里的“月-日 时:分”和该行引用文件的更新时间取最新）超过 N 小时 → 异常。
   状态含“卡 owner”“完成”“暂停”“归档”的行不查。
2. 该行引用的回报文件：要有“版本”，“已验证”那几行里至少一个证据路径；逐个看证据文件在不在（按项目根或回报所在目录找）。
3. 拦截仍生效：用守卫脚本对探针路径模拟一次写入（临时配置、真拦模式、日志写临时目录），应被拒；
   另看总控台设置里还挂着守卫钩子（只读 hooks 一项，不打印设置内容）。
4. 观察期：守卫是“只记录”模式且第一条记录已满 N 天 → 提醒总控问 owner 切不切，附本来会拦的条数。
去重：同一问题只报一次，问题消失后清掉；去重状态写在项目外（默认 %LOCALAPPDATA%/collab_patrol/）。
5–8 文档超限（字数一律按字符数，只数数，上限读项目根 文档上限.json，没有就用内置默认值；归档/ 不读不计）：
   5 状态文件字数／模板外标题／最近完成过期行与行数（≤10）／风险条数（≤5）；
   6 活跃区份数、单文件超限、关闭超期、检验无结论、重复文件（活跃区目录名读 文档上限.json 的 active_dirs，
   默认 ["改动记录","检验"]：第 1 个放任务包／回报／方案，第 2 个放检验子目录；缺的目录跳过）；
   7 决策记录条数；8 规则体量（仅配置有 rules_budget 时）。--docs-only 只跑 5–7。
限制：第 3 条只能证明“守卫脚本和配置还对、设置里还挂着”，证明不了平台仍在调用钩子。
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKIP_STATUS = ("卡 owner", "卡owner", "完成", "暂停", "归档")
PATH_RE = re.compile(r"`([^`\s]+)`")
EVID_EXT = (".md", ".py", ".json", ".jsonl", ".log", ".txt", ".html", ".js", ".css")


def table_rows(text: str, heading: str):
    """取某个二级标题下第一张表：返回 (表头列表, 行列表)。"""
    m = re.search(rf"^##\s*{re.escape(heading)}.*?$(.*?)(?=^##\s|\Z)", text, re.M | re.S)
    if not m:
        return [], []
    lines = [l for l in m.group(1).splitlines() if l.strip().startswith("|")]
    if len(lines) < 2:
        return [], []
    split = lambda l: [c.strip() for c in l.strip().strip("|").split("|")]
    head = split(lines[0])
    return head, [split(l) for l in lines[2:]]


def parse_when(s: str, now: datetime):
    m = re.search(r"(?<!\d)(?:(\d{4})-)?(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?(?!\d)", s)
    if not m:
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone(timedelta(hours=8)))
    year, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
    h, mi = (int(m.group(4)), int(m.group(5))) if m.group(4) else (0, 0)
    candidates = []
    years = [int(year)] if year else range(max(1, now.year - 4), min(9999, now.year + 4) + 1)
    for y in years:
        try:
            candidates.append(datetime(y, mo, d, h, mi, tzinfo=now.tzinfo))
        except ValueError:
            continue
    return min(candidates, key=lambda t: (abs((t - now).total_seconds()), t)).timestamp() if candidates else None


def verified_block(text: str) -> str:
    """只取明确标为“已验证”的节或条目；代码示例不作为回报证据。"""
    out, mode, level, fence = [], None, 0, None
    for line in text.splitlines():
        s = line.lstrip()
        ind = len(line) - len(s)
        fm = re.match(r"(`{3,}|~{3,})", s)
        if fence:
            if fm and fm[1][0] == fence[0] and len(fm[1]) >= fence[1]:
                fence = None
            continue
        if fm:
            fence = (fm[1][0], len(fm[1]))
            continue
        heading = re.match(r"^(#{1,6})\s+(.+?)(?:\s+#+)?\s*$", s)
        item = re.match(r"^(?:[-+*]|\d+[.)、])\s+", s)
        label = (heading[2] if heading else s[item.end():] if item else s).replace("**", "")
        label = re.sub(r"^(?:[一二三四五六七八九十\d]+[、.．]\s*)", "", label)
        verified = re.match(r"^已验证(?:\s*[:：（(]|\s*$)", label)
        other = re.match(r"^(?:结论|版本|未验证|失败项|待决定事项|退出证据|可上线)(?:\s*[:：]|\s*$)", label)
        if mode == "heading" and heading and len(heading[1]) <= level:
            mode = None
        elif mode == "item" and s and ind <= level:
            mode = None
        elif mode == "section" and (heading or other):
            mode = None
        if other and (heading or s.startswith("**")):
            mode = None
        if verified:
            mode, level = ("heading", len(heading[1])) if heading else ("item", ind) if item else ("section", 0)
            out.append(line)
        elif mode:
            out.append(line)
    return "\n".join(out)


def evidence_paths(text: str, root: Path, need_slash: bool = False):
    out = []
    for line in text.splitlines():
        for match in PATH_RE.finditer(line):
            p = match[1].replace("\\", "/")
            if not ("/" in p or (p.endswith(EVID_EXT) and not need_slash)) or p.startswith(("~", "http")) or "<" in p:
                continue
            if need_slash:
                if any(x in p for x in ("…", "...", ">", "$", "%")):
                    continue
                # 按短句判断路径用途，避免同一条目的排除说明吞掉别处的正式证据。
                before = re.split(r"[。；;，,]", line[:match.start()])[-1]
                after = re.split(r"[。；;，,]", line[match.end():])[0]
                context = PATH_RE.sub("", before + match[0] + after)
                if re.search(r"示例|例如|举例|路径样例|未创建|没有创建|不存在|排除|不在范围|不含|不包含", context):
                    continue
                # 完整的项目证据路径可以指向测试材料；假环境内的简写不能按真实项目核对。
                path = Path(p)
                if path.parts and all(part in (".", "..") for part in path.parts):
                    continue
                project_path = not path.is_absolute() and not p.startswith(".") and (root / path.parts[0]).is_dir()
                test_context = re.search(r"假\s*HOME|假用户目录|测试环境|测试目录|测试项目|项目副本|仿真环境|测试\s*cwd", context, re.I)
                test_relative = re.search(r"(?:假\s*HOME|测试环境|测试目录|测试项目|项目副本)(?:内|下|中)?的?\s*$", before, re.I)
                if not path.is_absolute() and test_context and (test_relative or not project_path):
                    continue
            out.append(p.rstrip("/"))
    return out


UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
LOG_TS_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def recent_lines(f: Path, now: datetime, days: float = 7) -> int:
    """错误日志里最近 N 天的非空行数；行首没有时间戳的照数（时间戳是本机时间 time.strftime 产生，按本机时区还原再和 now 比）。"""
    n = 0
    for l in f.read_text(encoding="utf-8", errors="replace").splitlines():
        if not l.strip():
            continue
        m = LOG_TS_RE.match(l)
        if m:
            try:
                ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()  # 本机时区
            except ValueError:
                ts = None
            if ts is not None and now.timestamp() - ts > days * 86400:
                continue
        n += 1
    return n


DISPATCH_END = "<!-- 调度区到此为止 -->"
DEFAULT_LIMITS = {
    "status_dispatch_max": 1900,      # 状态文件：调度区（开头到标记行）字数
    "status_total_max": 3800,         # 状态文件：全文字数
    "status_headings": ["现在在哪", "任务池", "待 owner 决定", "持续约束", "运行资源",
                        "最近完成", "风险", "决策记录"],  # 模板里允许的二级标题（按开头匹配）
    "recent_days": 7,                 # “最近完成”保留天数
    "changes_max_files": 15,          # 改动记录/ 份数
    "task_package_max": 3000,
    "report_max": 6000,
    "plan_max_lines": 120,            # 其他 .md（方案）非空行数
    "conclusion_max": 1500,     # 检验/**/结论.md
    "closed_days": 7,                 # 关闭多久后任务包／回报应移归档
    "evidence_days": 14,              # 检验子目录多久没结论就报
    "evidence_skip_dirs": ["巡检"],
    "decision_max": 50,
    "recent_max_rows": 10,            # “最近完成”表最多行数
    "risk_max_items": 5,              # “风险”节最多条数
    "active_dirs": ["改动记录", "检验"],  # 活跃区：[0]＝任务包／回报／方案所在，[1]＝检验子目录所在
}
DOC_DONE_RE = re.compile(r"(?:完成时间|关闭时间|完成于|关闭于|完成)\s*[:：]\s*(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}-\d{1,2})")


def load_limits(root: Path, issues: dict, path: str = "") -> dict:
    lim = json.loads(json.dumps(DEFAULT_LIMITS))
    f = Path(path) if path else root / "文档上限.json"
    if f.is_file():
        try:
            user = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(user, dict):
                raise ValueError("不是对象")
            lim.update({k: v for k, v in user.items() if not k.startswith("_")})
        except (OSError, ValueError, UnicodeError):
            issues["doc:limits"] = "【巡检】文档上限.json 损坏，本次按内置默认值检查｜看：文档上限.json"
    ad = lim.get("active_dirs")
    if not (isinstance(ad, list) and ad and all(isinstance(x, str) and x.strip() for x in ad)):
        lim["active_dirs"] = DEFAULT_LIMITS["active_dirs"]
        issues["doc:limits:active_dirs"] = "【巡检】文档上限.json 的 active_dirs 不是非空字符串列表，本次按默认值检查｜看：文档上限.json"
    return lim


def active_files(d: Path):
    """目录下所有文件；归档、缓存目录一律不读不计。"""
    if not d.is_dir():
        return []
    return [f for f in sorted(d.rglob("*")) if f.is_file()
            and not any(p in ("归档", "__pycache__") for p in f.relative_to(d).parts)]


def nchars(t: str) -> int:
    """文档上限一律按字符数算（Python len(str)；注意 Git Bash 的 wc -m 在本机给的是字节，不是这个数）。"""
    return len(t)


def md_text(f: Path) -> str:
    return f.read_text(encoding="utf-8", errors="replace")


def docs_checks(root: Path, status_name: str, lim: dict, now: datetime, issues: dict, status_text: str, pool: str = "任务池"):
    now_ts = now.timestamp()
    day = 86400

    def rel(p: Path) -> str:
        return p.relative_to(root).as_posix()

    # 5 状态文件
    if status_text:
        lines = status_text.splitlines()
        idx = next((i for i, l in enumerate(lines) if l.strip() == DISPATCH_END), None)
        if idx is None:
            issues["doc:status:marker"] = f"【巡检】文档超限｜状态文件缺调度区标记（需有原样一行 {DISPATCH_END}）｜看：{status_name}"
        else:
            n = nchars("\n".join(lines[:idx]))
            if n > lim["status_dispatch_max"]:
                issues["doc:status:dispatch"] = (f"【巡检】文档超限｜状态文件调度区 {n} 字，上限 {lim['status_dispatch_max']}，"
                                                 f"超 {n - lim['status_dispatch_max']} 字｜看：{status_name}")
        n = nchars(status_text)
        if n > lim["status_total_max"]:
            issues["doc:status:total"] = (f"【巡检】文档超限｜状态文件全文 {n} 字，上限 {lim['status_total_max']}，"
                                          f"超 {n - lim['status_total_max']} 字｜看：{status_name}")
        allowed = lim["status_headings"]
        extra = [h for h in re.findall(r"^##\s+(.+?)\s*$", status_text, re.M) if not any(h.startswith(x) for x in allowed)]
        if extra:
            issues["doc:status:headings:" + "|".join(extra)] = f"【巡检】文档超限｜状态文件有模板外的二级标题：{'、'.join(extra)}｜看：{status_name}"
        head, rows = table_rows(status_text, "最近完成")
        tcol = next((i for i, h in enumerate(head) if "时间" in h), None)
        old = []
        for r in rows:
            cells = [r[tcol]] if tcol is not None and tcol < len(r) else r
            ts = next((t for t in (parse_when(c, now) for c in cells) if t), None)
            if ts is not None and (now_ts - ts) / day > lim["recent_days"]:
                old.append(r[0] if r else "?")
        real = [r for r in rows if any(c.strip(" —-") for c in r)]
        if len(real) > lim["recent_max_rows"]:
            issues["doc:status:recent_rows"] = (f"【巡检】文档超限｜状态文件“最近完成”有 {len(real)} 行，上限 {lim['recent_max_rows']}，"
                                                f"超 {len(real) - lim['recent_max_rows']} 行｜看：{status_name}")
        m = re.search(r"^##\s*风险.*?$(.*?)(?=^##\s|\Z)", status_text, re.M | re.S)
        if m:
            body = [l for l in m.group(1).splitlines() if l.strip()]
            n_risk = sum(1 for l in body if re.match(r"\s*(?:[-*+]|\d+[.)、])\s+\S", l))
            tl = [l for l in body if l.lstrip().startswith("|")]
            n_risk += max(0, len(tl) - 2) if tl else 0
            if n_risk == 0:
                n_risk = sum(1 for l in body if not l.lstrip().startswith((">", "#", "|")))
            if n_risk > lim["risk_max_items"]:
                issues["doc:status:risk"] = (f"【巡检】文档超限｜状态文件“风险”有 {n_risk} 条，上限 {lim['risk_max_items']}，"
                                             f"超 {n_risk - lim['risk_max_items']} 条｜看：{status_name}")
        if old:
            issues["doc:status:recent:" + "|".join(old)] = (f"【巡检】文档超限｜状态文件“最近完成”有 {len(old)} 行超过 {lim['recent_days']} 天"
                                                            f"（{'、'.join(old[:3])}{'等' if len(old) > 3 else ''}）｜看：{status_name}")

    # 6 活跃区
    ad = lim["active_dirs"]
    changes_dir = root / ad[0]
    check_dir = root / ad[1] if len(ad) > 1 else root / "__无检验区__"
    cname = ad[0] + "/"
    cfiles = active_files(changes_dir)
    if len(cfiles) > lim["changes_max_files"]:
        issues["doc:changes:count"] = (f"【巡检】文档超限｜{cname} 有 {len(cfiles)} 份，上限 {lim['changes_max_files']}，"
                                       f"超 {len(cfiles) - lim['changes_max_files']} 份｜看：{cname}")
    for f in cfiles:
        if f.suffix != ".md":
            continue
        t = md_text(f)
        if "任务包" in f.name:
            n, m, unit = nchars(t), lim["task_package_max"], "字"
        elif "回报" in f.name:
            n, m, unit = nchars(t), lim["report_max"], "字"
        else:
            n, m, unit = sum(1 for l in t.splitlines() if l.strip()), lim["plan_max_lines"], "行"
        if n > m:
            issues[f"doc:big:{rel(f)}"] = f"【巡检】文档超限｜单文件 {n} {unit}，上限 {m}｜看：{rel(f)}"
    for f in active_files(check_dir):
        if f.name == "结论.md":
            n = nchars(md_text(f))
            if n > lim["conclusion_max"]:
                issues[f"doc:big:{rel(f)}"] = f"【巡检】文档超限｜结论.md {n} 字，上限 {lim['conclusion_max']}｜看：{rel(f)}"

    # 关闭超 N 天仍在活跃区的任务包／回报
    done_rows, live = [], set()  # 关闭时间来源：最近完成表、任务池里已完成的行；在途行引用的文件不算关闭
    if status_text:
        for heading in ("最近完成", pool):
            head, rows = table_rows(status_text, heading)
            tcol = next((i for i, h in enumerate(head) if "时间" in h or "动静" in h), None)
            scol = head.index("状态") if "状态" in head else None
            for r in rows:
                text = " | ".join(r)
                closed = heading == "最近完成" or (scol is not None and scol < len(r) and any(k in r[scol] for k in SKIP_STATUS))
                if not closed:
                    live |= {(root / p).resolve() for p in evidence_paths(text, root)}
                    continue
                cells = [r[tcol]] if tcol is not None and tcol < len(r) else r
                ts = next((t for t in (parse_when(c, now) for c in cells) if t), None)
                if ts is not None:
                    done_rows.append((text, ts))
    for f in cfiles:
        if f.suffix != ".md" or not ("任务包" in f.name or "回报" in f.name) or f.resolve() in live:
            continue
        topic = re.sub(r"[-_]?(任务包|回报)$", "", re.sub(r"^\d{4}-\d{2}-\d{2}-", "", f.stem))
        closed_ts, how = None, ""
        for text, ts in done_rows:
            if f.name in text or (len(topic) >= 3 and topic in text):
                closed_ts = ts
                break
        if closed_ts is None:
            m = DOC_DONE_RE.search("\n".join(md_text(f).splitlines()[:60]))
            if m:
                closed_ts = parse_when(m.group(1), now)
        if closed_ts is None:
            closed_ts, how = f.stat().st_mtime, "（按修改时间估）"
        days = (now_ts - closed_ts) / day
        if days > lim["closed_days"]:
            issues[f"doc:closed:{rel(f)}"] = f"【巡检】文档超限｜关闭约 {days:.0f} 天仍在活跃区{how}，应移归档｜看：{rel(f)}"
    # 检验子目录超 N 天没有结论.md
    if check_dir.is_dir():
        for d in sorted(p for p in check_dir.iterdir() if p.is_dir() and p.name not in ("归档", *lim["evidence_skip_dirs"])):
            fs = active_files(d)
            if not fs or any(f.name == "结论.md" for f in fs):
                continue
            days = (now_ts - max(f.stat().st_mtime for f in fs)) / day
            if days > lim["evidence_days"]:
                issues[f"doc:evid:{rel(d)}"] = f"【巡检】文档超限｜检验目录 {days:.0f} 天没有改动且无结论.md，应压结论｜看：{rel(d)}/"
    # 活跃区两个目录里同名同大小的重复文件
    seen = {(f.name, f.stat().st_size): f for f in cfiles if f.stat().st_size}
    for f in active_files(check_dir):
        o = seen.get((f.name, f.stat().st_size))
        if o is not None:
            issues[f"doc:dup:{rel(f)}"] = f"【巡检】文档超限｜重复文件，与 {rel(o)} 同名同大小｜看：{rel(f)}"

    # 7 决策记录
    dec = root / "决策记录.md"
    if dec.is_file():
        rows = [l for l in md_text(dec).splitlines() if l.strip()]
        n = sum(1 for l in rows if re.match(r"\s*(?:[-*+]|\d+[.)、])\s+\S", l))
        tl = [l for l in rows if l.lstrip().startswith("|")]
        n += max(0, len(tl) - 2) if tl else 0  # 表格：去掉表头和分隔行
        if n > lim["decision_max"]:
            issues["doc:decision"] = f"【巡检】文档超限｜决策记录 {n} 条，上限 {lim['decision_max']}，超 {n - lim['decision_max']} 条｜看：决策记录.md"


def rules_check(lim: dict, issues: dict):
    """8 规则体量：只在配置里有 rules_budget 时跑；只数字数，不输出内容。"""
    rb = lim.get("rules_budget")
    if not isinstance(rb, dict):
        return
    unit = "字"
    base = Path(os.path.expanduser(rb.get("dir", "~/.claude")))
    items = [(base / n, b) for n, b in (rb.get("files") or {}).items()]
    cards = base / rb.get("cards_dir", "岗位卡")
    if cards.is_dir() and rb.get("card_max"):
        items += [(c, rb["card_max"]) for c in sorted(cards.glob("*.md"))]
    for f, b in items:
        if f.is_file():
            n = len(md_text(f))
            if n > b:
                issues[f"doc:rules:{f.name}"] = f"【巡检】规则超预算｜{f.name} {n} {unit}，预算 {b}，超 {n - b} {unit}｜看：{f.as_posix()}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--status", default="", help="状态文件相对项目根的路径；缺省读文档上限.json 的 status_path，再缺省用 状态.md；项目状态文件不叫这个名字时必须指定（也可在文档上限.json 写 status_path），否则状态文件相关检查全部跳过并报“读不到状态文件”")
    ap.add_argument("--stale-hours", type=float, default=5)
    ap.add_argument("--guard-config", default="总控台/.claude/collab_guard.json")
    ap.add_argument("--guard-script", default=str(Path(__file__).with_name("collab_guard.py")))
    ap.add_argument("--probe", default="规则/__巡检探针__.md")
    ap.add_argument("--observe-days", type=float, default=3)
    ap.add_argument("--state-dir", default=os.path.join(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()), "collab_patrol"))
    ap.add_argument("--limits", default="", help="上限配置文件路径，默认项目根 文档上限.json")
    ap.add_argument("--docs-only", action="store_true", help="只跑第 5–7 条文档超限检查（给没配巡检的项目手动用）")
    ap.add_argument("--now", default="")  # 演练用：指定当前时间 "YYYY-MM-DD HH:MM"
    ap.add_argument("--tz-offset", type=float, default=8, help="状态及 --now 的 UTC 小时偏移，默认北京时间 +8")
    a = ap.parse_args()

    root = Path(a.root).resolve()
    tz = timezone(timedelta(hours=a.tz_offset))
    now = datetime.strptime(a.now, "%Y-%m-%d %H:%M").replace(tzinfo=tz) if a.now else datetime.now(tz)
    now_ts = now.timestamp()
    issues = {}  # key -> 一行
    lim = load_limits(root, issues, a.limits)
    if not a.status:
        sp = lim.get("status_path")
        a.status = sp if isinstance(sp, str) and sp.strip() else "状态.md"
    ph = lim.get("pool_heading")
    pool = ph if isinstance(ph, str) and ph.strip() else "任务池"

    def core_checks():
        status_text = (root / a.status).read_text(encoding="utf-8")
        # 1、2：任务池（标题读 pool_heading，缺省“任务池”）
        head, rows = table_rows(status_text, pool)
        col = {h: i for i, h in enumerate(head)}
        for r in rows:
            if len(r) < len(head):
                continue
            name = r[col.get("事项", 0)]
            st = r[col["状态"]] if "状态" in col else ""
            if any(k in st for k in SKIP_STATUS):
                continue
            dep_ts = None
            wait_dep = "卡依赖" in st  # 等依赖的行：依赖列写了时间且到期才查停摆
            if wait_dep and "依赖" in col and col["依赖"] < len(r):
                dep_ts = parse_when(r[col["依赖"]], now)
            row_text = " | ".join(r)
            last = parse_when(r[col["最后动静"]], now) if "最后动静" in col else None
            files = [root / p for p in evidence_paths(row_text, root)]
            files = [f for f in files if f.is_file()]
            times = [t for t in [last] + [f.stat().st_mtime for f in files] if t]
            future_bad = last is not None and last - now_ts > 36 * 3600
            if future_bad:
                issues[f"time:{name}"] = f"【巡检】{name}｜时间写法异常（晚于现在超过 36 小时）｜看：任务池该行"
            dep_wait = wait_dep and (dep_ts is None or dep_ts > now_ts)  # 没写时间或还没到期：不报停摆
            if wait_dep and dep_ts is not None and dep_ts <= now_ts:
                times.append(dep_ts)  # 起点取最后动静和依赖时间中较晚的
            if times and not future_bad and not dep_wait:
                hours = (now_ts - max(times)) / 3600
                if hours > a.stale_hours:
                    if wait_dep:
                        issues[f"stale:{name}"] = f"【巡检】{name}｜依赖已到期 {hours:.0f} 小时，没有动静（阈值 {a.stale_hours:g} 小时）｜看：任务池该行"
                    else:
                        issues[f"stale:{name}"] = f"【巡检】{name}｜{hours:.0f} 小时没有动静（阈值 {a.stale_hours:g} 小时）｜看：任务池该行"
            for f in files:
                if "回报" not in f.name:
                    continue
                t = f.read_text(encoding="utf-8", errors="replace")
                verified = verified_block(t)
                ev = evidence_paths(verified, root, need_slash=True)  # 光写文件名的不算（多半是别的项目或简写）
                rel = f.relative_to(root).as_posix()
                if "版本" not in t or not ev:
                    issues[f"noevid:{rel}"] = f"【巡检】{name}｜回报缺{'版本' if '版本' not in t else '“已验证”里的证据路径'}｜看：{rel}"
                else:
                    bases = [root, f.parent]
                    miss = sorted(e for e in ev if not any((b / e).exists() for b in bases))
                    if miss:
                        issues[f"missing:{rel}:{miss[0]}"] = (f"【巡检】{name}｜回报“已验证”里的证据路径不存在：{miss[0]}"
                                                             f"{f' 等 {len(miss)} 处' if len(miss) > 1 else ''}｜看：{rel}")

        # 3：拦截仍生效
        gcfg_path = root / a.guard_config
        try:
            gcfg = json.loads(gcfg_path.read_text(encoding="utf-8"))
            if not isinstance(gcfg, dict):
                raise ValueError("守卫配置不是对象")
        except (OSError, ValueError, UnicodeError):
            gcfg = None
            issues["guard:config"] = f"【巡检】拦截未配置或配置损坏｜看：{a.guard_config}"
        if gcfg is not None:
            desk = gcfg_path.parent.parent
            with tempfile.TemporaryDirectory() as td:
                tcfg = dict(gcfg, mode="enforce", log=str(Path(td) / "probe.jsonl"),
                            project_root=str(root))
                tpath = Path(td) / "cfg.json"
                tpath.write_text(json.dumps(tcfg, ensure_ascii=False), encoding="utf-8")
                inp = {"session_id": "patrol", "cwd": str(desk), "tool_name": "Write",
                       "tool_input": {"file_path": str(root / a.probe)}}
                r = subprocess.run([sys.executable, "-I", a.guard_script, "--config", str(tpath)],
                                   input=json.dumps(inp, ensure_ascii=False).encode("utf-8"),
                                   capture_output=True, cwd=str(desk))
                if '"deny"' not in r.stdout.decode("utf-8", "replace"):
                    issues["guard:probe"] = f"【巡检】拦截失效｜探针 {a.probe} 没被守卫拒绝｜看：{a.guard_config}"
            sp = desk / ".claude" / "settings.json"
            try:
                hooks = json.loads(sp.read_text(encoding="utf-8")).get("hooks", {})
                hooked = "collab_guard" in json.dumps(hooks.get("PreToolUse", []))
            except Exception:
                hooked = False
            if not hooked:
                issues["guard:settings"] = "【巡检】拦截失效｜总控台设置里没找到守卫钩子｜看：总控台/.claude/settings.json"

            # 4：观察期
            log = (desk / gcfg.get("log", "拦截记录.jsonl")) if not os.path.isabs(gcfg.get("log", "")) else Path(gcfg["log"])
            if gcfg.get("mode", "observe") == "observe" and log.exists():
                recs = []  # 只取“窗口”是 UUID 的真实记录；坏行、测试记录（自拟窗口名）一律跳过
                for l in log.read_text(encoding="utf-8", errors="replace").splitlines():
                    try:
                        x = json.loads(l)
                        if not (isinstance(x, dict) and UUID_RE.match(str(x.get("窗口", "")))):
                            continue
                        x["_ts"] = datetime.strptime(str(x["时间"]), "%Y-%m-%d %H:%M:%S").timestamp()
                    except (ValueError, KeyError, TypeError):
                        continue
                    recs.append(x)
                if recs:
                    # 守卫日志由本机 time.strftime 产生，按本机时区还原绝对时间。
                    days = (now_ts - min(x["_ts"] for x in recs)) / 86400
                    if days >= a.observe_days:
                        n = sum(1 for x in recs if x.get("本来会拦"))
                        u = sum(1 for x in recs if x.get("规则") == "无法解析")
                        issues["observe:due"] = (f"【巡检】观察期到期｜已记录 {days:.0f} 天，本来会拦 {n} 条、命令解析不了 {u} 条，"
                                                 f"请总控汇总误拦后问 owner 切不切真拦｜看：总控台/{gcfg.get('log', '拦截记录.jsonl')}")

        # 守卫自身出错（出错时一律放行，所以要单独报；含写入守卫和派单守卫两个日志）
        errlog = Path.home() / ".claude" / "collab" / "guard_errors.log"
        if errlog.exists():
            n_err = recent_lines(errlog, now)
            if n_err:
                issues[f"guard:errors:{n_err}"] = f"【巡检】守卫出错累计 {n_err} 次（最近 7 天，出错时会放行）｜看：~/.claude/collab/guard_errors.log"
        derr = Path.home() / ".claude" / "collab" / "dispatch_guard_errors.log"
        if derr.exists():
            n_d = recent_lines(derr, now)
            if n_d:
                issues[f"guard:dispatch_errors:{n_d}"] = f"【巡检】派单守卫出错累计 {n_d} 次（最近 7 天，出错时会放行，派单没被核对）｜看：~/.claude/collab/dispatch_guard_errors.log"


    if not a.docs_only:
        core_checks()
    status_path = root / a.status
    status_text = status_path.read_text(encoding="utf-8") if status_path.is_file() else ""
    if not status_path.is_file():
        issues["doc:status:missing"] = f"【巡检】读不到状态文件 {a.status}（本项目状态文件不叫这个名字时，加 --status <相对路径>），状态文件相关检查没跑｜看：--status"
    docs_checks(root, a.status, lim, now, issues, status_text, pool)
    if not a.docs_only:
        rules_check(lim, issues)

    # 去重
    sd = Path(a.state_dir)
    sd.mkdir(parents=True, exist_ok=True)
    sf = sd / (hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:12] + ("-docs" if a.docs_only else "") + ".json")
    seen = set(json.loads(sf.read_text(encoding="utf-8"))) if sf.exists() else set()
    for k, line in issues.items():
        if k not in seen:
            print(line)
    sf.write_text(json.dumps(sorted(issues), ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except Exception as e:
        print(f"【巡检】巡检脚本出错（{type(e).__name__}）｜看：巡检定时任务")
        sys.exit(1)
