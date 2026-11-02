
"""FleetPulse Intelligence agent: Ollama + bounded analytics tools."""

import json
import os
from pathlib import Path
from typing import Any

import duckdb
import ollama

from src.agent.prompts import SYSTEM_PROMPT
from src.agent.tools import TOOL_FUNCTIONS


# Resolve the database relative to the project root.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / "data" / "warehouse" / "fleetpulse.duckdb"

DEFAULT_MODEL = "llama3.2:3b"
MODEL = os.getenv("OLLAMA_MODEL", DEFAULT_MODEL)
MAX_TOOL_ROUNDS = 6


# Only expose predefined analytics functions to the LLM.
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_platform_overview",
            "description": (
                "Compare overall platform activity across current "
                "and prior reporting periods."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_segments",
            "description": (
                "Compare customer bridge classifications and stable "
                "customer activity across regions and subscription tiers."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyse_feature_usage",
            "description": (
                "Investigate feature adoption and feature-level event "
                "changes across reporting periods."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_customer_risk",
            "description": (
                "Examine customer usage-reduction buckets and associated "
                "latest-month MRR exposure."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyse_decline_timing",
            "description": (
                "Analyse Reports and Dashboards activity in rolling "
                "seven-day blocks."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_data_quality",
            "description": (
                "Run warehouse data-quality checks and retrieve "
                "the latest recorded event date."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_metric_definition",
            "description": (
                "Retrieve the definition of a metric such as DAU, "
                "WAU, MAU, MRR, retention or feature adoption. "
                "Omit metric to retrieve all definitions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "metric": {
                        "type": "string",
                        "description": "Metric name to look up.",
                    }
                },
            },
        },
    },
]


def _serialise(data: Any) -> str:
    """Convert tool results into JSON-safe text."""
    return json.dumps(data, default=str, indent=2)


def execute_tool(
    tool_name: str,
    arguments: dict | None = None,
    connection=None,
) -> Any:
    """Execute one explicitly allowlisted analytics tool."""

    arguments = arguments or {}

    if tool_name not in TOOL_FUNCTIONS:
        return {"error": f"Tool not permitted: {tool_name}"}

    tool_function = TOOL_FUNCTIONS[tool_name]

    try:
        if tool_name == "get_metric_definition":
            return tool_function(**arguments)

        if connection is None:
            raise RuntimeError("Database connection is unavailable.")

        if arguments:
            return {
                "error": "This analytics tool does not accept arguments."
            }

        return tool_function(connection)

    except Exception as exc:
        return {
            "error": f"Tool execution failed: {type(exc).__name__}: {exc}"
        }


def ask_fleetpulse(
    question: str,
    model: str = MODEL,
    max_tool_rounds: int = MAX_TOOL_ROUNDS,
) -> dict:
    """
    Answer a FleetPulse analytics question using Ollama
    and the predefined DuckDB analytics tools.

    Returns:
        dict containing answer, model, and tool execution history.
    """

    if not isinstance(question, str) or not question.strip():
        raise ValueError("Please provide a non-empty question.")

    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT,
        },
        {
            "role": "user",
            "content": question.strip(),
        },
    ]

    tool_history = []
    connection = None

    try:
        for round_number in range(max_tool_rounds + 1):

            response = ollama.chat(
                model=model,
                messages=messages,
                tools=TOOL_SCHEMAS,
                options={"temperature": 0.1},
            )

            assistant_message = response.message
            messages.append(assistant_message)

            tool_calls = assistant_message.tool_calls or []

            # No further tools requested: return the final answer.
            if not tool_calls:
                answer = assistant_message.content or (
                    "The agent did not generate a response."
                )

                return {
                    "answer": answer,
                    "model": model,
                    "tools_used": tool_history,
                    "tool_call_count": len(tool_history),
                }

            if round_number >= max_tool_rounds:
                break

            for tool_call in tool_calls:

                tool_name = tool_call.function.name
                arguments = dict(tool_call.function.arguments or {})

                # Open the warehouse only when a database tool is needed.
                if (
                    tool_name in TOOL_FUNCTIONS
                    and tool_name != "get_metric_definition"
                    and connection is None
                ):
                    if not DB_PATH.exists():
                        result = {
                            "error": f"Warehouse not found: {DB_PATH}"
                        }
                    else:
                        connection = duckdb.connect(
                            str(DB_PATH),
                            read_only=True,
                        )
                        result = execute_tool(
                            tool_name,
                            arguments,
                            connection,
                        )
                else:
                    result = execute_tool(
                        tool_name,
                        arguments,
                        connection,
                    )

                tool_history.append(
                    {
                        "tool": tool_name,
                        "arguments": arguments,
                        "result": result,
                    }
                )

                messages.append(
                    {
                        "role": "tool",
                        "content": _serialise(result),
                    }
                )

        return {
            "answer": (
                "The investigation reached its tool-call limit. "
                "Please narrow the question or investigate it in stages."
            ),
            "model": model,
            "tools_used": tool_history,
            "tool_call_count": len(tool_history),
        }

    finally:
        if connection is not None:
            connection.close()


def main():
    """Run FleetPulse Intelligence from the command line."""

    print("\nFleetPulse Intelligence")
    print(f"Model: {MODEL}")
    print("Type 'exit' to quit.\n")

    while True:
        question = input("You: ").strip()

        if question.lower() in {"exit", "quit"}:
            print("Goodbye.")
            break

        if not question:
            continue

        try:
            result = ask_fleetpulse(question)

            print("\nFleetPulse:")
            print(result["answer"])

            if result["tools_used"]:
                print("\nTools used:")
                for item in result["tools_used"]:
                    print(f"- {item['tool']}")

            print()

        except Exception as exc:
            print(f"\nAgent error: {exc}\n")


if __name__ == "__main__":
    main()
