"""A tiny text-to-SQL agent — the "existing AI agent" that Nishiki measures.

Given a natural-language business question and a database schema, it asks a model to write ONE SQLite
SELECT query that answers it. A downstream app would then run that query and show the rows — this is the
kind of self-service-analytics agent teams run thousands of times a month, where the model's cost and
its answer-correctness (does the query return the right rows?) both matter a lot.

The single function every model call passes through is `generate_sql` — that is the "choke point".
It's an ordinary OpenAI-style `chat.completions.create` call (pointed at OpenRouter), so
`nishiki init` finds it automatically and splices candidate models in at measure time (in memory;
this file is never edited). See `injection.choke: agent:generate_sql` in `.nishiki/KOI.yaml`.

Run it directly to try one question against a real model + the sample DB:

    pip install openai
    export OPENROUTER_API_KEY=sk-or-...
    python agent.py "How many products cost 500 dollars or more?"
"""
import os
import sqlite3
import sys

from openai import OpenAI

# The model this agent uses today. Nishiki swaps this for each candidate at the choke; the value
# here is just the agent's own default when you run it standalone.
DEFAULT_MODEL = os.environ.get("SQL_AGENT_MODEL", "z-ai/glm-4.6")

PROMPT_TEMPLATE = (
    "You are a SQL analyst. Given a SQLite database schema and a question, write ONE SQLite "
    "SELECT query that answers it. Output only the SQL — no explanation, no markdown fences.\n\n"
    "Schema:\n{schema}\n\nQuestion: {question}\n\nSQL:"
)


def db_schema(db_path):
    """Read the CREATE TABLE statements from the SQLite DB (the schema the model writes SQL against)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL ORDER BY name"
        ).fetchall()
    finally:
        conn.close()
    return "\n".join(r[0] for r in rows)


def generate_sql(question, schema, model=DEFAULT_MODEL):
    """The choke point: send one chat completion and return the response with usage.

    Returns a dict {text, model, input_tokens, output_tokens}. Carrying the token usage on the
    return value is what lets Nishiki's live probe read the real token counts off the call with
    zero configuration (no second request) — see `nishiki run`.
    """
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.environ.get("OPENROUTER_API_KEY", ""),
    )
    prompt = PROMPT_TEMPLATE.format(question=question, schema=schema)
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
    if not argv:
        print("usage: python agent.py <question>", file=sys.stderr)
        return 2
    here = os.path.dirname(os.path.abspath(__file__))
    schema = db_schema(os.path.join(here, "shop.db"))
    r = generate_sql(argv[0], schema)
    print(r["text"])
    print(f"[{r['input_tokens']} in / {r['output_tokens']} out tokens]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
