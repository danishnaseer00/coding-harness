import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


@dataclass
class EvalTask:
    name: str
    prompt: str
    expected_pattern: Optional[str] = None
    forbidden_pattern: Optional[str] = None
    expected_tools: Optional[list[str]] = None
    min_steps: Optional[int] = None
    max_steps: Optional[int] = None
    expected_in_output: Optional[list[str]] = None
    tags: list[str] = field(default_factory=list)


@dataclass
class EvalResult:
    task_name: str
    passed: bool
    output: str
    steps: int
    duration: float
    tools_called: list[str]
    checks: dict[str, bool] = field(default_factory=dict)
    error: Optional[str] = None

    def summary(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return f"[{status}] {self.task_name} ({self.steps} steps, {self.duration:.1f}s)"


@dataclass
class BenchmarkReport:
    results: list[EvalResult]
    total_duration: float

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.passed)

    @property
    def total(self) -> int:
        return len(self.results)

    def print(self):
        for r in self.results:
            print(f"  {r.summary()}")
        print(f"\nResults: {self.passed}/{self.total} passed ({self.failed} failed) in {self.total_duration:.1f}s")

    def to_json(self, path: str):
        data = {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "total_duration": self.total_duration,
            "results": [
                {
                    "task": r.task_name,
                    "passed": r.passed,
                    "steps": r.steps,
                    "duration": r.duration,
                    "error": r.error,
                }
                for r in self.results
            ],
        }
        Path(path).write_text(json.dumps(data, indent=2))


AgentFactory = Callable[[], object]


async def run_eval(agent_factory: AgentFactory, task: EvalTask) -> EvalResult:
    agent = agent_factory()
    start = time.time()
    try:
        output = await agent.run(task.prompt)
    except Exception as e:
        return EvalResult(
            task_name=task.name, passed=False, output="",
            steps=getattr(agent, "_step_count", 0),
            duration=time.time() - start,
            tools_called=[], error=str(e),
        )
    duration = time.time() - start
    steps = agent._step_count

    tools_called = _extract_tools(agent.messages)
    checks = {}

    checks["expected_pattern"] = True
    if task.expected_pattern:
        checks["expected_pattern"] = bool(re.search(task.expected_pattern, output, re.IGNORECASE))

    checks["forbidden_pattern"] = True
    if task.forbidden_pattern:
        checks["forbidden_pattern"] = not re.search(task.forbidden_pattern, output, re.IGNORECASE)

    checks["expected_tools"] = True
    if task.expected_tools:
        checks["expected_tools"] = all(t in tools_called for t in task.expected_tools)

    checks["min_steps"] = True
    if task.min_steps is not None:
        checks["min_steps"] = steps >= task.min_steps

    checks["max_steps"] = True
    if task.max_steps is not None:
        checks["max_steps"] = steps <= task.max_steps

    checks["expected_in_output"] = True
    if task.expected_in_output:
        checks["expected_in_output"] = all(s.lower() in output.lower() for s in task.expected_in_output)

    passed = all(checks.values())

    return EvalResult(
        task_name=task.name, passed=passed, output=output,
        steps=steps, duration=duration,
        tools_called=tools_called, checks=checks,
    )


async def run_benchmark(agent_factory: AgentFactory, tasks: list[EvalTask]) -> BenchmarkReport:
    start = time.time()
    results = []
    for task in tasks:
        result = await run_eval(agent_factory, task)
        results.append(result)
    return BenchmarkReport(results=results, total_duration=time.time() - start)


def _extract_tools(messages: list) -> list[str]:
    tools = []
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") in ("tool_use", "tool_call"):
                    tools.append(block.get("name", ""))
    return tools
