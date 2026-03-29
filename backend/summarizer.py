# backend/summarizer.py
import json
import os
import logging
from openai import AsyncOpenAI
from backend.metrics import (
    LLM_PROMPT_TOKENS,
    LLM_COMPLETION_TOKENS,
    LLM_TOTAL_TOKENS,
)
logging.basicConfig(level=logging.INFO)

# Instantiate a single async client (reads OPENAI_API_KEY from env)
API_KEY = os.getenv("OPENAI_API_KEY")
if not API_KEY:
    raise ValueError("OPENAI_API_KEY environment variable is not set.")

client = AsyncOpenAI(api_key=API_KEY)

# Default “system” prompt for summarization 
DEFAULT_PROMPT = (
    """
You are a highly reliable and deterministic Kubernetes AI SRE assistant.

You will be provided with raw Kubernetes pod logs and related diagnostic details. Your ONLY job is to analyze and return a strict, valid JSON output. Carefully follow the instructions below with zero deviation:

INSTRUCTIONS:
1. Analyze current and previous pod logs, last termination reason, and associated events.
2. Identify and clearly explain the **root cause** of failure (if determinable).
3. Summarize important issues: errors, warnings, crash loops, connection failures, resource limits, etc.
4. Extract the **single most critical error line**.
   - If the logs contain a Python **Traceback**, include the **entire traceback block** from "Traceback" down to the last indented line. This must go under the `key_error_line` field.
   - Do **not** summarize or truncate traceback blocks. Include them **verbatim**, even if long.
5. Ignore "pod not found" or "previous container not found" messages unless they indicate an actual failure.
6. Identify the type of failure (e.g., SecretError, OOMKilled, ConnectionError, RuntimeException).
7. If there are messages about the current pod being missing and logs fetched from a replacement or new pod, mention that, including pod names.
8. Detect OOMKilled or memory-related terminations from logs, events, or termination reasons and include them in the analysis.

FORMATTING RULES (STRICT):
- You MUST return a **single valid JSON object** only.
- type_of_failure must be in Capitalized CamelCase (e.g., "OOMKilled", "ConnectionError").
- **No markdown**, **no code blocks**, **no additional text** outside the JSON.
- JSON keys:
  - `type_of_failure`: Must be in **CapitalCamelCase** (also known as PascalCase). Valid examples: "OOMKilled", "ConnectionError", "Unknown", "RuntimeFailure" etc.
  - `analysis`: A concise root cause explanation.
  - `key_error_line`: The most critical error line or full traceback block (if present).
  - `summary`: [ A short, Slack-friendly list (array of bullet points) explaining what happened in 3–5 ].
- Escape all special characters properly.
- If unsure, use empty strings (`""`) or empty arrays (`[]`).
- Ensure **all brackets and quotes are properly closed**.
- The summary field must always be a properly closed JSON array with 3–5 strings and Ensure the array ends with a closing square bracket.
- The output MUST be **100% valid JSON** with **no surrounding explanation**.

EXPECTED JSON FORMAT (MANDATORY):

{
  "type_of_failure": "",
  "analysis": "",
  "key_error_line": "",
  "summary": [""]
}

"""
)

async def summarize_logs(logs: str, prompt: str = None) -> str:
    """
    Summarize raw logs using the OpenAI Chat Completions API (async).
    Tries 'gpt-5' first, and falls back to other GPT Model if invalid JSON returned.
    Handles exceptions gracefully.
    """
    system_message = prompt or DEFAULT_PROMPT

    async def call_model(model_name: str) -> str:
        logging.info(f"Requesting summary from model {model_name}...")
        response = await client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": logs},
            ],
            temperature=0.1,
            max_completion_tokens=800,
            #max_tokens=800,
        )

        # record token usage 
        usage = getattr(response, "usage", None)
        if usage:
            # safe getattr with default 0
            prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
            completion_tokens = getattr(usage, "completion_tokens", 0) or 0
            total_tokens = getattr(usage, "total_tokens", None)
            if total_tokens is None:
                total_tokens = prompt_tokens + completion_tokens

            LLM_PROMPT_TOKENS.labels(model=model_name).inc(prompt_tokens)
            LLM_COMPLETION_TOKENS.labels(model=model_name).inc(completion_tokens)
            LLM_TOTAL_TOKENS.labels(model=model_name).inc(total_tokens)
        else:
            logging.info(f"No token usage info returned by model {model_name}")

        return response.choices[0].message.content

    def is_valid_json(text: str) -> bool:
        try:
            json.loads(text)
            return True
        except json.JSONDecodeError:
            return False

    try:
        output = await call_model("gpt-4o")
        if not is_valid_json(output):
            logging.warning("Invalid JSON received from gpt-4o, retrying with gpt-4.1")
            output = await call_model("gpt-4.1")
    
            if not is_valid_json(output):
                logging.warning("Invalid JSON received from gpt-4.1, retrying with gpt-5")
                output = await call_model("gpt-5")

                if not is_valid_json(output):
                    logging.warning("Invalid JSON received from gpt-5, retrying with o4-mini")
                    output = await call_model("o4-mini")
    
        return output
    
    except Exception as e:
        logging.error(f"An unexpected error occurred during summarization: {str(e)}")
        raise e  # Propagate exception upwards

