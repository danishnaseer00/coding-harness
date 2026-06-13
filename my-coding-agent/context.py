def clip(text: str, limit: int = 1500) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f"\n... [{len(text) - limit} chars omitted] ...\n" + text[-half:]


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
