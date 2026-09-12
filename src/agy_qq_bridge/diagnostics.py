"""
本地系统与工作区诊断模块
提供零 Token 消耗的毫秒级 Git 状态检查与本地诊断能力
"""
import asyncio
from pathlib import Path


async def get_local_git_status(workspace: Path) -> str:
    """
    通过子进程在当前绑定的 AGY 工作区执行 git status，零 Token 消耗瞬间返回
    """
    if not workspace.exists():
        return f"⚠️ 当前配置的工作区目录不存在：`{workspace}`"

    try:
        # 1. 获取当前分支名
        proc_branch = await asyncio.create_subprocess_exec(
            "git", "branch", "--show-current",
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, _ = await proc_branch.communicate()
        branch = stdout_b.decode("utf-8", errors="replace").strip() or "HEAD (detached)"

        # 2. 获取简要状态
        proc_status = await asyncio.create_subprocess_exec(
            "git", "status", "-s",
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_s, stderr_s = await proc_status.communicate()

        if proc_status.returncode != 0:
            err_msg = stderr_s.decode("utf-8", errors="replace").strip()
            if "not a git repository" in err_msg.lower():
                return f"📁 工作区 `{workspace}` 不是 Git 代码仓库。"
            return f"⚠️ 执行 `git status` 失败: {err_msg}"

        status_text = stdout_s.decode("utf-8", errors="replace").strip()
        if not status_text:
            return (
                f"📁 **Git 工作区状态**\n\n"
                f"• **工作区**: `{workspace}`\n"
                f"• **当前分支**: `{branch}`\n"
                f"• **变动状态**: 干净（Working tree clean，无任何待提交修改）"
            )

        lines = [l for l in status_text.splitlines() if l.strip()]
        display_lines = "\n".join(lines[:25])
        if len(lines) > 25:
            display_lines += f"\n... (其余 {len(lines) - 25} 项已折叠)"

        return (
            f"📁 **Git 工作区状态**\n\n"
            f"• **工作区**: `{workspace}`\n"
            f"• **当前分支**: `{branch}` ({len(lines)} 项变动)\n\n"
            f"```text\n{display_lines}\n```\n"
            f"💡 *提示：若需让 AI 审查或提交代码，可直接输入对话「帮我提交以上变动」*"
        )
    except Exception as e:
        return f"⚠️ 检查工作区失败: {e}"
