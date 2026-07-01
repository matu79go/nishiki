"""A tiny span-extraction agent — the "existing AI agent" that Nishiki measures.

Given a question and a passage of text, it asks a model to return the *verbatim* fragment of the
passage that answers the question (no paraphrase). This is a non-classification task: the quality
signal is span-overlap F1 against a gold answer, not a right/wrong label.

The single function every model call passes through is `extract_span` — that is the "choke point".
It's an ordinary OpenAI-style `chat.completions.create` call (pointed at OpenRouter), so
`nishiki init` finds it automatically and splices candidate models in at measure time (in memory;
this file is never edited). See `injection.choke: agent:extract_span` in `.nishiki/KOI.yaml`.

Run it directly to try one example against a real model:

    pip install openai
    export OPENROUTER_API_KEY=sk-or-...
    python agent.py "Which ocean does the Amazon River empty into?" \
        "The Amazon River ... empties into the Atlantic Ocean near the equator."
"""
import os
import sys

from openai import OpenAI

# The model this agent uses today. Nishiki swaps this for each candidate at the choke; the value
# here is just the agent's own default when you run it standalone.
DEFAULT_MODEL = os.environ.get("SPAN_AGENT_MODEL", "z-ai/glm-4.6")

PROMPT_TEMPLATE = (
    "Extract the exact span of text from the passage that answers the question. "
    "Copy it verbatim from the passage — do not paraphrase or add words. "
    "If the passage does not answer the question, reply with NONE.\n\n"
    "Question: {question}\n\nPassage:\n{context}\n\nAnswer span:"
)


def extract_span(question, context, model=DEFAULT_MODEL):
    """The choke point: send one chat completion and return the response with usage.

    Returns a dict {text, model, input_tokens, output_tokens}. Carrying the token usage on the
    return value is what lets Nishiki's live probe read the real token counts off the call with
    zero configuration (no second request) — see `nishiki run`.
    """
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.environ.get("OPENROUTER_API_KEY", ""),
    )
    prompt = PROMPT_TEMPLATE.format(question=question, context=context)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    )
    usage = resp.usage
    return {
        "text": (resp.choices[0].message.content or "").strip(),
        "model": model,
        "input_tokens": getattr(usage, "prompt_tokens", 0),
        "output_tokens": getattr(usage, "completion_tokens", 0),
    }


def main(argv):
    if len(argv) < 2:
        print("usage: python agent.py <question> <passage>", file=sys.stderr)
        return 2
    r = extract_span(argv[0], argv[1])
    print(r["text"])
    print(f"[{r['input_tokens']} in / {r['output_tokens']} out tokens]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
