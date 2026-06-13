import sys


def bold(s: str) -> str:
    return f"\033[1m{s}\033[0m"


def dim(s: str) -> str:
    return f"\033[2m{s}\033[0m"


def cyan(s: str) -> str:
    return f"\033[36m{s}\033[0m"


def green(s: str) -> str:
    return f"\033[32m{s}\033[0m"


def yellow(s: str) -> str:
    return f"\033[33m{s}\033[0m"


def red(s: str) -> str:
    return f"\033[31m{s}\033[0m"


def magenta(s: str) -> str:
    return f"\033[35m{s}\033[0m"


def print_assistant(text: str):
    print()
    for line in text.strip().splitlines():
        print(f"  {line}")
    print()


def print_tool_call(name: str, args: dict):
    args_str = ", ".join(f"{k}={v}" for k, v in args.items())
    print(f"  {cyan(bold('>>'))} {bold(name)}({args_str})")


def print_tool_result(name: str, result: str, duration: float = 0):
    preview = result.strip()[:200]
    dur = f" ({duration:.1f}s)" if duration else ""
    print(f"  {dim('└─')} {dim(result.split(chr(10))[0] if result else '(empty)')}{dim(dur)}")


def print_error(msg: str):
    print(f"  {red('x')} {msg}", file=sys.stderr)
