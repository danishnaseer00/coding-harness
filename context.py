def clip(text: str, limit: int = 1500) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f"\n... [{len(text) - limit} chars omitted] ...\n" + text[-half:]


async def summarize_old_messages(messages: list, provider) -> list:
    if len(messages) <= 20:
        return messages

    split = 10
    for i in range(min(split + 2, len(messages))):
        content = messages[i].get("content", "")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") in ("tool_use", "tool_result"):
                    if i + 1 < len(messages):
                        nxt = messages[i + 1].get("content", "")
                        if isinstance(nxt, list):
                            nxt_types = {b.get("type") for b in nxt if isinstance(b, dict)}
                            if "tool_use" in nxt_types or "tool_result" in nxt_types:
                                split = max(split, i + 2)
                    break

    to_summarize = messages[:split]
    keep = messages[split:]

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
