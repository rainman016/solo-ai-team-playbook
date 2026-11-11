"""总控台写入守卫（PreToolUse 钩子）：总控只能写白名单路径；先只记录，到期切真拦。

挂法（总控台/.claude/settings.json）：
  "hooks": {"PreToolUse": [{"matcher": "Edit|Write|MultiEdit|NotebookEdit|Bash",
    "hooks": [{"type": "command",
      "command": "python -I \"<HOME>/.claude/collab/collab_guard.py\""}]}]}
  配置由环境变量 CLAUDE_PROJECT_DIR 指向的项目目录自动定位，无需外壳展开。

配置（collab_guard.json，路径相对配置文件所在的总控台/.claude/ 的上一级，即总控台/）：
  {"project_root": "..",                     项目根目录
   "writable": ["状态.md", "改动记录/", "归档/", "总控台/"],  项目内允许写的（文件或目录/）
   "protected": ["总控台/.claude/"],          即使在白名单里也不许写（守卫自己的配置）
   "mode": "observe",                         observe 只记录；enforce 真拦
   "log": "拦截记录.jsonl"}                    日志，相对总控台/

约定：
- 只管项目根目录以内的写入；项目外（如上线写 ~/.claude/）不管。
- 日志只记时间、窗口、工具、动作、路径、规则、本来会不会拦、模式，不记文件内容和整条命令。
- 守卫自身出错一律放行（只在日志里记错误类型），不因脚本问题卡住总控。
- 命令行写入按常见写法解析（重定向、tee、sed -i、cp/mv/install、rm/touch/mkdir/rmdir）；
  python 等脚本内部写文件解析不到，靠规则约束。
"""
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SEPARATORS = {"&&", "||", ";", "|", "&", "\n"}
REDIRECTS = {">", ">>", ">|", "&>", "&>>"}


def norm(p: str, base: Path) -> Path:
    """把 git-bash / Windows / ~ / 相对路径统一成绝对路径。"""
    p = p.strip().strip('"').strip("'")
    p = os.path.expandvars(os.path.expanduser(p))
    m = re.match(r"^/([a-zA-Z])/(.*)$", p)  # /c/Users/... -> <盘符>:/<用户目录>/...
    if m and os.name == "nt":
        p = f"{m.group(1).upper()}:/{m.group(2)}"
    q = Path(p)
    if not q.is_absolute():
        q = base / q
    return Path(os.path.abspath(q))


def key(p: Path) -> str:
    return os.path.normcase(str(p))


def strip_comment(line: str) -> str:
    """按 shell 规则去掉注释：引号外、位于词首的 # 到行尾。词中的 #（如 a#b）保留。"""
    q, i = None, 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and q != "'":  # 转义：跳过下一个字符
            i += 2
            continue
        if q:
            if ch == q:
                q = None
        elif ch in "'\"":
            q = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t;&|()"):
            return line[:i]
        i += 1
    return line


def bash_targets(cmd: str, cwd: Path) -> list[tuple[str, str, Path]]:
    """返回 [(动作, 目标路径字符串, 解析基准目录)]。跳过 heredoc 正文；跟踪 cd；引号跨行时合并几行再解析。"""
    lines, kept, skip_until = cmd.split("\n"), [], None
    for line in lines:
        if skip_until is not None:
            if line.strip() == skip_until:
                skip_until = None
            continue
        line = strip_comment(line)
        kept.append(line)
        m = re.search(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?", line)
        if m:
            skip_until = m.group(1)
    out, base, k = [], cwd, 0
    while k < len(kept):
        buf, toks = kept[k], None
        k += 1
        while True:
            try:
                lex = shlex.shlex(buf, posix=True, punctuation_chars=True)
                lex.whitespace_split = True
                lex.commenters = ""  # 不把 # 当注释，免得 a#b 后面的写入被吞掉
                toks = list(lex)
                break
            except ValueError:
                if k < len(kept):  # 引号跨行：并上下一行再试
                    buf += "\n" + kept[k]
                    k += 1
                else:
                    break
        # 子命令、括号分组、反引号：目标和 cd 范围判断不准，整段记“无法解析”（不拦，进统计）
        if toks is None or any(t in {"(", ")"} or "(" in t or ")" in t or "`" in t for t in toks):
            out.append(("命令行（无法解析）", "", base))
            continue
        cmds, cur = [], []
        for t in toks:
            if t in SEPARATORS:
                cmds.append(cur)
                cur = []
            else:
                cur.append(t)
        cmds.append(cur)
        for c in cmds:
            i, words = 0, []
            while i < len(c):
                t = c[i]
                if t in REDIRECTS and i + 1 < len(c):
                    out.append(("命令行重定向写入", c[i + 1], base))
                    i += 2
                    continue
                if t in {"<", "<<", "<<<"}:
                    i += 2
                    continue
                words.append(t)
                i += 1
            if words and words[-1].isdigit():
                words = words[:-1]
            if not words:
                continue
            name = os.path.basename(words[0])
            args = [w for w in words[1:] if not w.startswith("-")]
            if name in {"cd", "pushd"}:
                base = norm(args[0], base) if args else Path.home()
            elif name == "tee":
                out += [("命令行 tee 写入", a, base) for a in args]
            elif name == "sed" and any(w.startswith("-i") or w == "--in-place" for w in words[1:]):
                out += [("命令行 sed -i 改写", a, base) for a in args[1:]]
            elif name in {"cp", "mv", "install"} and args:
                out.append((f"命令行 {name} 写入", args[-1], base))
            elif name in {"rm", "touch", "mkdir", "rmdir"}:
                out += [(f"命令行 {name}", a, base) for a in args]
    return out

def main() -> int:
    argv = sys.argv[1:]
    proj = os.environ.get("CLAUDE_PROJECT_DIR", "")
    default_cfg = Path(proj) / ".claude" / "collab_guard.json" if proj else Path(".claude/collab_guard.json")
    cfg_path = Path(os.path.expandvars(argv[argv.index("--config") + 1])) if "--config" in argv else default_cfg
    data = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    cwd = Path(data.get("cwd") or os.getcwd())
    if not cfg_path.is_absolute():
        cfg_path = cwd / cfg_path
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    desk = cfg_path.parent.parent  # 总控台/
    root = norm(cfg.get("project_root", ".."), desk)
    writable = [norm(w, root) for w in cfg.get("writable", [])]
    protected = [norm(w, root) for w in cfg.get("protected", [])]
    mode = cfg.get("mode", "observe")
    log_path = norm(cfg.get("log", "拦截记录.jsonl"), desk)

    tool = data.get("tool_name", "")
    ti = data.get("tool_input") or {}
    if tool in EDIT_TOOLS:
        p = ti.get("file_path") or ti.get("notebook_path") or ""
        targets = [(f"{tool} 编辑", p, cwd)] if p else []
    elif tool == "Bash":
        targets = bash_targets(ti.get("command", ""), cwd)
    else:
        return 0

    records, block_reasons = [], []
    for action, raw, base in targets:
        if not raw:
            rec_path, inside, ok = "", False, True
        else:
            ap = norm(raw, base)
            k, rk = key(ap), key(root)
            inside = k == rk or k.startswith(rk + os.sep)
            rel = os.path.relpath(ap, root) if inside else ""

            def under(lst):
                return any(k == key(w) or k.startswith(key(w).rstrip(os.sep) + os.sep) for w in lst)
            is_protected = inside and under(protected)
            ok = (not inside) or (under(writable) and not is_protected)
            rec_path = str(rel).replace("\\", "/") if inside else "项目外"
        would_block = inside and not ok
        records.append({
            "时间": time.strftime("%Y-%m-%d %H:%M:%S"),
            "窗口": data.get("session_id", ""),
            "工具": tool,
            "动作": action,
            "路径": rec_path,
            "规则": ("受保护（守卫配置）" if raw and is_protected else "不在总控可写范围") if would_block else ("项目外，不管" if raw and not inside else ("无法解析" if not raw else "允许")),
            "本来会拦": would_block,
            "模式": mode,
        })
        if would_block:
            block_reasons.append(rec_path)

    if records:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    if block_reasons and mode == "enforce":
        reason = ("总控台只允许写：" + "、".join(cfg.get("writable", []))
                  + f"。被拦：{'、'.join(block_reasons)}。这活该派独立窗口，不换写法去改（总控专用规则·新项目起步）。")
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except Exception as e:  # 守卫出错一律放行，只记错误类型
        try:
            err = Path.home() / ".claude" / "collab" / "guard_errors.log"
            err.parent.mkdir(parents=True, exist_ok=True)
            with open(err, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {type(e).__name__}\n")
        except Exception:
            pass
        sys.exit(0)
