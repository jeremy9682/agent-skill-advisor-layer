#!/usr/bin/env python3
"""Codex/Claude skill discovery-budget check.

Codex 官方口径: skill 元数据(name+description)预算约为上下文窗口的 2%,
窗口未知时 8,000 字符;超预算先缩短 description,严重时省略并警告。
(https://developers.openai.com/codex/skills)

本脚本测量本机各 skill 根目录的元数据占用,标出最大的 description,
并对照 8k 保守预算与 2% 估算给出结论。只读,不改任何文件。

用法: python3 scripts/discovery_budget_check.py [--context-tokens N] [--json]
"""
import argparse
import glob
import json
import os
import re
import sys

CODEX_ROOTS = [
    ("~/.agents/skills", "codex-official-user"),
    ("~/.codex/skills", "codex-legacy-compat"),
]
CLAUDE_ROOTS = [
    ("~/.claude/skills", "claude-personal"),
    ("~/.claude/commands", "claude-commands"),
]
PER_SKILL_OVERHEAD = 40  # 路径/分隔等固定开销的保守估计(字符)


def parse_frontmatter(path):
    """Return (name, description) from a SKILL.md/command file, best-effort."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(32768)
    except OSError:
        return None, None
    m = re.match(r"\s*---\n(.*?)\n---", text, re.S)
    if not m:
        return None, None
    fm = m.group(1)

    def field(key):
        fm_m = re.search(rf"^{key}:\s*(.*)$", fm, re.M)
        if not fm_m:
            return None
        val = fm_m.group(1).strip()
        if val in (">", "|", ">-", "|-"):  # block scalar: collect indented lines
            lines = []
            after = fm[fm_m.end():].split("\n")
            for ln in after[1:] if after and after[0] == "" else after:
                if ln.startswith((" ", "\t")):
                    lines.append(ln.strip())
                elif ln.strip() == "":
                    continue
                else:
                    break
            val = " ".join(lines)
        return val.strip("'\"")

    return field("name"), field("description")


def scan_root(root, label):
    root = os.path.expanduser(root)
    entries = []
    if not os.path.isdir(root):
        return {"root": root, "label": label, "exists": False, "skills": []}
    candidates = glob.glob(os.path.join(root, "*", "SKILL.md")) + [
        p for p in glob.glob(os.path.join(root, "*.md"))
        if os.path.basename(p) not in ("README.md",)
    ]
    for p in sorted(set(candidates)):
        name, desc = parse_frontmatter(p)
        if name is None and desc is None:
            continue
        name = name or os.path.basename(os.path.dirname(p))
        desc = desc or ""
        entries.append({
            "name": name,
            "path": p,
            "desc_chars": len(desc),
            "meta_chars": len(name) + len(desc) + PER_SKILL_OVERHEAD,
        })
    return {"root": root, "label": label, "exists": True, "skills": entries}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--context-tokens", type=int, default=353400,
                    help="Codex 会话上下文窗口(token),用于 2%% 估算;默认取本机实测 session_meta 值")
    ap.add_argument("--chars-per-token", type=float, default=3.5,
                    help="token→字符换算(混合中英保守值)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    codex = [scan_root(r, l) for r, l in CODEX_ROOTS]
    claude = [scan_root(r, l) for r, l in CLAUDE_ROOTS]

    codex_total = sum(s["meta_chars"] for r in codex for s in r["skills"])
    codex_count = sum(len(r["skills"]) for r in codex)
    budget_fallback = 8000
    budget_2pct = int(args.context_tokens * 0.02 * args.chars_per_token)

    result = {
        "codex": {
            "roots": [{"label": r["label"], "root": r["root"], "exists": r["exists"],
                        "count": len(r["skills"]),
                        "meta_chars": sum(s["meta_chars"] for s in r["skills"])}
                       for r in codex],
            "count": codex_count,
            "meta_chars_total": codex_total,
            "budget_fallback_chars": budget_fallback,
            "budget_2pct_est_chars": budget_2pct,
            "over_fallback": codex_total > budget_fallback,
            "over_2pct_est": codex_total > budget_2pct,
            "top_descriptions": sorted(
                (s for r in codex for s in r["skills"]),
                key=lambda s: -s["desc_chars"])[:15],
        },
        "claude_info_only": [{"label": r["label"], "count": len(r["skills"]),
                               "meta_chars": sum(s["meta_chars"] for s in r["skills"])}
                              for r in claude],
    }

    if args.json:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=1)
        return

    c = result["codex"]
    print("== Codex 侧 skill 发现预算 ==")
    for r in c["roots"]:
        print(f"  {r['label']:22s} {r['root']}: {r['count']} 个, {r['meta_chars']:,} 字符" if r["exists"]
              else f"  {r['label']:22s} {r['root']}: (不存在)")
    print(f"  合计 {c['count']} 个 skill, 元数据约 {c['meta_chars_total']:,} 字符")
    print(f"  预算: 未知窗口保守值 {c['budget_fallback_chars']:,} 字符 -> {'超出' if c['over_fallback'] else '未超'}")
    print(f"        2% x {args.context_tokens:,} tokens 估算 {c['budget_2pct_est_chars']:,} 字符 -> {'超出' if c['over_2pct_est'] else '未超'}")
    if c["over_fallback"]:
        print("  结论: 超出保守预算 — description 会被压缩/省略, 低频 skill 建议缩短描述、合并单步骤 skill 或显式化(allow_implicit_invocation: false)")
    print("  Top description(字符):")
    for s in c["top_descriptions"][:10]:
        print(f"    {s['desc_chars']:5d}  {s['name']}  ({s['path']})")
    print("== Claude 侧(信息参考; Claude 的预算由 skillListingBudgetFraction 管理) ==")
    for r in result["claude_info_only"]:
        print(f"  {r['label']:22s} {r['count']} 个, {r['meta_chars']:,} 字符")


if __name__ == "__main__":
    main()
