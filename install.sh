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

# Check if any provider API key is set
if [ -z "$TOKENROUTER_API_KEY" ] && [ -z "$ANTHROPIC_API_KEY" ] && [ -z "$OPENAI_API_KEY" ] && [ -z "$GROQ_API_KEY" ] && [ -z "$OPENROUTER_API_KEY" ]; then
    echo ""
    echo "⚠️  No API keys found. Set at least one in your ~/.bashrc or ~/.zshrc:"
    echo "  export TOKENROUTER_API_KEY=your_key_here     (default provider)"
    echo "  export ANTHROPIC_API_KEY=your_key_here"
    echo "  export OPENAI_API_KEY=your_key_here"
    echo "  export GROQ_API_KEY=your_key_here"
    echo "  export OPENROUTER_API_KEY=your_key_here"
fi

echo ""
echo "✅ Installation complete. Run:"
echo "  harness --cwd /path/to/your/project"
echo ""
echo "To use a different provider, set CODING_HARNESS_PROVIDER environment variable:"
echo "  export CODING_HARNESS_PROVIDER=anthropic  # or: groq, openai, tokenrouter, etc."
