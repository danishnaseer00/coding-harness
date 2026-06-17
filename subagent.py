import asyncio

SUBAGENT_TIMEOUT = 30


async def run_subagent(
    task: str,
    context: str,
    *,
    cwd: str,
    provider,
    callbacks: dict | None = None,
    depth: int = 0,
) -> str:
    """Spawn a focused read-only sub-agent for a subtask.

    The sub-agent can read files, search code, and explore the workspace
    but CANNOT write files or run shell commands.

    Max nesting depth is 1 — sub-agents cannot spawn sub-agents.
    A timeout of SUBAGENT_TIMEOUT seconds prevents hanging.

    Returns the sub-agent's output prefixed with ``[Sub-agent result]``.
    """
    if depth >= 1:
        return "error: subagents cannot spawn subagents (max depth reached)"

    prompt = _build_subagent_prompt(task, context)

    from agent import Agent  # lazy import to avoid circular dependency

    child = Agent(
        cwd=cwd,
        approval_policy="never",
        read_only=True,
        max_steps=5,
        depth=depth + 1,
        provider=provider,
        callbacks=callbacks or {},
        system_prompt=prompt,
    )

    try:
        result = await asyncio.wait_for(child.run(task), timeout=SUBAGENT_TIMEOUT)
    except asyncio.TimeoutError:
        return (
            "[Sub-agent result]\n"
            f"error: sub-agent timed out after {SUBAGENT_TIMEOUT}s on task: {task[:100]}"
        )
    except Exception as e:
        return (
            "[Sub-agent result]\n"
            f"error: sub-agent failed: {e}"
        )

    return f"[Sub-agent result]\n{result}"


def _build_subagent_prompt(task: str, context: str) -> str:
    from guardrails import guardrail_rules

    return (
        "You are a focused sub-agent. Complete the following task using the available tools.\n"
        f"\n"
        f"Task: {task}\n"
        f"{f'Context: {context}' if context else ''}\n"
        f"\n"
        f"You can read files, search code, and explore the workspace "
        f"but CANNOT write files or run shell commands.\n"
        f"\n"
        f"Enforced guardrails:\n"
        f"{guardrail_rules()}\n"
        f"\n"
        f"Report back your findings concisely. When done, provide a clear summary."
    )
