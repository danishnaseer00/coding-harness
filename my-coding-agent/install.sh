#!/bin/bash
set -e

echo "Installing Coding Harness..."

if ! command -v python3 &>/dev/null; then
    echo "Error: Python 3.10+ required"
    exit 1
fi

PY_VERSION=$(python3 -c "import sys; print(sys.version_info.minor)")
if [ "$PY_VERSION" -lt 10 ]; then
    echo "Error: Python 3.10+ required (found 3.$PY_VERSION)"
    exit 1
fi

pip install -e .

if [ -z "$ANTHROPIC_API_KEY" ]; then
    echo ""
    echo "No ANTHROPIC_API_KEY found."
    echo "Set one of these in your ~/.bashrc or ~/.zshrc:"
    echo "  export ANTHROPIC_API_KEY=your_key_here"
    echo "  export OPENAI_API_KEY=your_key_here"
    echo "  export GROQ_API_KEY=your_key_here"
    echo "  export OPENROUTER_API_KEY=your_key_here"
fi

echo ""
echo "Done. Run: harness --cwd /path/to/your/project"
