"""全局派单钩子安装：只往 Claude Code 设置文件的 hooks.PreToolUse 追加一条派单守卫。

用法（owner 当次明确授权后，由项目总控执行）：
    python -I add_global_dispatch_hook.py [设置文件路径] [--dry-run]
默认路径 ~/.claude/settings.json。追加的条目：
    matcher "Agent|Task|mcp__ccd_session__start_session" → python -I "<用户目录>/.claude/collab/dispatch_guard.py"

装钩子的顺序（缺了脚本，钩子命令退出码 2＝拦截，所有会话的派单都会被拦死）：
    先把 dispatch_guard.py 和 派单定档.json 放进 ~/.claude/collab/ → 验 `echo {} | python -X utf8 -I <脚本>` 退出码为 0 → 再装钩子；
    以后撤脚本或改名前，先撤钩子。
本脚本写入前自动检查：脚本和表都在、表是含 roles 的 JSON、脚本空输入退出码为 0；任一不满足就拒装（试运行也检查），什么也不写。

安全约定（同 add_gate_ask.py）：
- 不打印设置文件的任何内容（含 env 段），只打印条数和备份路径；
- 文本级最小插入：其他字节（含 BOM、换行、缩进）原样保留；
- 已有 command 含 dispatch_guard.py 的 PreToolUse 条目就不重复加；
- hooks 或 PreToolUse 不存在时新建；
- 改前把原文件按字节备份到 ~/.claude/备份/（备份同样含 env 段，按密钥文件对待：不读、不打印、不外传）；
- 先写同目录临时文件、自检通过、写前重读确认文件没被别人改动，再整体替换；写后校验为合法 JSON；
- 解析失败时什么也不写，只报“解析失败”。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MATCHER = "Agent|Task|mcp__ccd_session__start_session"
MARK = "dispatch_guard.py"


def entry() -> dict:
    script = (Path.home() / ".claude" / "collab" / "dispatch_guard.py").as_posix()
    return {"matcher": MATCHER, "hooks": [{"type": "command", "command": f'python -I "{script}"'}]}


def precheck() -> str:
    """装钩子前的就位检查：返回空串＝通过，否则返回一句拒装原因。"""
    base = Path.home() / ".claude" / "collab"
    script, table = base / "dispatch_guard.py", base / "派单定档.json"
    missing = [p.name for p in (script, table) if not p.is_file()]
    if missing:
        return f"{'、'.join(missing)} 不在 {base.as_posix()}/ 里"
    try:
        t = json.loads(table.read_text(encoding="utf-8"))
        if not isinstance(t, dict) or not isinstance(t.get("roles"), dict):
            raise ValueError
    except (OSError, ValueError, UnicodeError):
        return "派单定档.json 不是含 roles 的合法 JSON"
    try:
        r = subprocess.run([sys.executable, "-X", "utf8", "-I", str(script)], input=b"{}", capture_output=True,
                           cwd=tempfile.gettempdir(), timeout=30)
    except (OSError, subprocess.SubprocessError):
        return "dispatch_guard.py 空输入试运行失败"
    if r.returncode != 0:
        return f"dispatch_guard.py 空输入试运行退出码是 {r.returncode}（要 0）"
    return ""


def unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("对象含重复键，不能安全定位")
        obj[key] = value
    return obj


def reject_constant(value):
    raise ValueError("非标准 JSON 常量")


DECODER = json.JSONDecoder(object_pairs_hook=unique_object, parse_constant=reject_constant)


def skip_space(text, pos):
    while pos < len(text) and text[pos] in " \t\r\n":
        pos += 1
    return pos


def members(text, start):
    """合法对象中每个值的原文范围，以及最后一个值结束处；不改原文。"""
    pos, end, spans = skip_space(text, start + 1), start + 1, {}
    while text[pos] != "}":
        key, pos = DECODER.raw_decode(text, pos)
        pos = skip_space(text, pos)
        if text[pos] != ":":
            raise ValueError("对象结构无法安全定位")
        value_start = skip_space(text, pos + 1)
        _, end = DECODER.raw_decode(text, value_start)
        spans[key] = (value_start, end)
        pos = skip_space(text, end)
        if text[pos] == "}":
            break
        if text[pos] != ",":
            raise ValueError("对象分隔符无法安全定位")
        pos = skip_space(text, pos + 1)
    return spans, end


def plan_insert(raw):
    """返回 (插入后的字节, 是否新增)；不写文件，不重排任何已有文本。"""
    bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
    text = raw.decode("utf-8-sig")
    data = DECODER.decode(text)
    if not isinstance(data, dict):
        raise ValueError("顶层不是对象")
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks 不是对象")
    pre = hooks.get("PreToolUse", [])
    if not isinstance(pre, list):
        raise ValueError("hooks.PreToolUse 不是列表")
    if MARK in json.dumps(pre, ensure_ascii=False):
        return raw, False

    enc = json.dumps(entry(), ensure_ascii=False)
    root, root_end = members(text, skip_space(text, 0))
    if "hooks" not in root:
        pos = root_end
        insertion = (", " if root else "") + '"hooks": {"PreToolUse": [' + enc + "]}"
    else:
        hspans, hend = members(text, root["hooks"][0])
        if "PreToolUse" not in hspans:
            pos = hend
            insertion = (", " if hspans else "") + '"PreToolUse": [' + enc + "]"
        else:
            start, _ = hspans["PreToolUse"]
            pos = start + 1
            cursor = skip_space(text, pos)
            while text[cursor] != "]":
                _, pos = DECODER.raw_decode(text, cursor)
                cursor = skip_space(text, pos)
                if text[cursor] == "]":
                    break
                if text[cursor] != ",":
                    raise ValueError("数组分隔符无法安全定位")
                cursor = skip_space(text, cursor + 1)
            insertion = (", " if pre else "") + enc

    result = bom + (text[:pos] + insertion + text[pos:]).encode("utf-8")
    expected = dict(data)
    expected["hooks"] = dict(hooks, PreToolUse=pre + [entry()])
    if DECODER.decode(result.decode("utf-8-sig")) != expected:
        raise ValueError("插入后的结构自检失败")
    return result, True


def main() -> int:
    unknown = [a for a in sys.argv[1:] if a.startswith("-") and a != "--dry-run"]
    if unknown:
        print("不认识的参数，未做任何修改。只支持 --dry-run。")
        return 1
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    if len(args) > 1:
        print("参数数量错误，未做任何修改。只接受一个设置文件路径。")
        return 1
    dry_run = "--dry-run" in sys.argv[1:]
    path = Path(args[0]) if args else Path.home() / ".claude" / "settings.json"

    original_exists = path.exists()
    raw = path.read_bytes() if original_exists else b"{}"
    try:
        result, added = plan_insert(raw)
    except (ValueError, UnicodeError, RecursionError):
        print("解析或安全定位失败：需为无注释、无重复键的标准 JSON，且 hooks 为对象、PreToolUse 为列表；未做任何修改。")
        return 1
    print("派单钩子：已装过，无需新增。" if not added else "派单钩子：待新增 1 条。")
    if added:
        bad = precheck()
        if bad:
            print(f"拒装：{bad}。先把 dispatch_guard.py 和 派单定档.json 放进 ~/.claude/collab/ 并验空输入退出码为 0，再装钩子；未做任何修改。")
            return 1
    if dry_run or not added:
        print("未写入（试运行或无需新增）。")
        return 0

    backup = None
    if path.exists():
        backup_dir = Path.home() / ".claude" / "备份"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_fd, backup_name = tempfile.mkstemp(dir=backup_dir, prefix=f"{path.name}.{time.time_ns()}.", suffix=".bak")
        os.close(backup_fd)
        backup = Path(backup_name)
        shutil.copy2(path, backup)

    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".tmp-hook-", delete=False) as f:
            tmp = Path(f.name)
            f.write(result)
        if tmp.read_bytes() != result:
            raise ValueError("临时文件字节校验失败")
        try:
            current_raw = path.read_bytes()
        except FileNotFoundError:
            current_raw = None
        if current_raw != (raw if original_exists else None):
            if backup is not None:
                backup.unlink()
            print("设置文件在处理期间被改动，未写入，请重跑")
            return 1
        os.replace(tmp, path)
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink()
    print("已写入 1 条；写后校验为合法 JSON。")
    if backup:
        print(f"备份：{backup}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        code = main()
    except Exception as e:  # 不打印调用栈和异常内容，免得带出文件片段
        print(f"出错（{type(e).__name__}），已停止；如已打印备份路径，可用备份恢复。")
        code = 2
    sys.exit(code)
