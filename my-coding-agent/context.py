from tools import TOOL_HANDLERS


def clip(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f"\n... [{len(text) - limit} chars omitted] ...\n" + text[-half:]


def history_text(history: list, max_chars: int = 12000) -> str:
    if not history:
        return ""

    recent = history[-6:]
    older = history[:-6]

    parts = []

    for entry in older:
        role = entry["role"]
        content = clip(str(entry.get("content", "")), limit=500)
        parts.append(f"[{role}]: {content}")

    for entry in recent:
        role = entry["role"]
        content = clip(str(entry.get("content", "")), limit=2000)
        parts.append(f"[{role}]: {content}")

    result = "\n\n".join(parts)
    return clip(result, max_chars)


def deduplicate_reads(history: list) -> list:
    seen_files = set()
    result = []
    for entry in history:
        if entry.get("tool") == "read_file":
            path = entry.get("args", {}).get("path")
            if path in seen_files:
                entry = {**entry, "content": f"(read of {path} deduplicated)"}
            else:
                seen_files.add(path)
        result.append(entry)
    return result


async def summarize_old_messages(messages: list, provider) -> list:
    if len(messages) <= 20:
        return messages

    to_summarize = messages[:10]
    keep = messages[10:]

    summary_text = "\n".join(
        f"[{m['role']}]: {str(m.get('content', ''))[:200]}"
        for m in to_summarize
    )

    try:
        response = await provider.send(
            messages=[{"role": "user", "content": f"Summarize these messages in 3-5 sentences:\n{summary_text}"}],
            system_prompt="You summarize conversation history concisely.",
            tools=[]
        )
        summary = response.text or "(summary failed)"
    except Exception:
        summary = "(summarization unavailable)"

    summary_message = {
        "role": "user",
        "content": f"[Summary of earlier conversation]: {summary}"
    }
    return [summary_message] + keep
