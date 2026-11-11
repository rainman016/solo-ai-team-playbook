"""门禁弹框：只往 Claude Code 设置文件追加 permissions.ask 条目。

用法（owner 当次明确授权后，由项目总控执行）：
    python -I add_gate_ask.py [设置文件路径] [--dry-run]
默认路径 ~/.claude/settings.json。

安全约定：
- 不打印设置文件的任何内容（含 env 段），只打印条数和备份路径；
- 只插入 permissions.ask 缺少的条目，其他字节（含 BOM、换行、缩进）原样保留；
- 改前把原文件按字节备份到 ~/.claude/备份/（备份同样含 env 段，按密钥文件对待：不读、不打印、不外传）；
- 先写临时文件，自检通过后整体替换原文件；
- 解析失败时什么也不写，只报“解析失败”。
"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

GATE_RULES = [
    "Bash(git push *)",
    "Bash(git merge *)",
    "Bash(gh pr merge *)",
    "Bash(gh repo create *)",
    "Bash(gh repo edit *)",
    "Bash(gh release create *)",
]


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
    """返回单次插入后的字节和新增条目；不写文件，不重排任何已有文本。"""
    bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
    text = raw.decode("utf-8-sig")
    data = DECODER.decode(text)
    if not isinstance(data, dict):
        raise ValueError("顶层不是对象")
    perms = data.get("permissions", {})
    if not isinstance(perms, dict):
        raise ValueError("permissions 不是对象")
    ask = perms.get("ask", [])
    if not isinstance(ask, list):
        raise ValueError("permissions.ask 不是列表")
    added = [r for r in GATE_RULES if r not in ask]
    if not added:
        return raw, added

    encoded = ", ".join(json.dumps(r, ensure_ascii=False) for r in added)
    root, root_end = members(text, skip_space(text, 0))
    if "permissions" not in root:
        pos = root_end
        insertion = (", " if root else "") + '"permissions": {"ask": [' + encoded + "]}"
    else:
        pspans, pend = members(text, root["permissions"][0])
        if "ask" not in pspans:
            pos = pend
            insertion = (", " if pspans else "") + '"ask": [' + encoded + "]"
        else:
            start, _ = pspans["ask"]
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
            insertion = (", " if ask else "") + encoded

    result = bom + (text[:pos] + insertion + text[pos:]).encode("utf-8")
    # 自检期望值；任何不一致都在备份、临时文件和正式写入之前拒绝。
    expected = dict(data)
    expected["permissions"] = dict(perms, ask=ask + added)
    if DECODER.decode(result.decode("utf-8-sig")) != expected:
        raise ValueError("插入后的结构自检失败")
    return result, added


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
        print("解析或安全定位失败：需为无注释、无重复键的标准 JSON，且 permissions 为对象、ask 为列表；未做任何修改。")
        return 1
    print(f"已有门禁条目 {len(GATE_RULES) - len(added)} 条，待新增 {len(added)} 条。")
    if dry_run or not added:
        print("未写入（试运行或无需新增）。")
        return 0

    backup = None
    if path.exists():
        backup_dir = Path.home() / ".claude" / "备份"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_fd, backup_name = tempfile.mkstemp(
            dir=backup_dir, prefix=f"{path.name}.{time.time_ns()}.", suffix=".bak"
        )
        os.close(backup_fd)
        backup = Path(backup_name)
        shutil.copy2(path, backup)

    # 先写同目录临时文件、自检通过后整体替换，避免写到一半
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".tmp-gate-", delete=False) as f:
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
    print(f"已写入 {len(added)} 条；写后校验为合法 JSON。")
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
