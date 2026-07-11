import json
import os

import anthropic
from flask import Flask, Response, render_template, request

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

PORT = int(os.environ.get("PORT", "3004"))
MAX_ARG_CHARS = 8000  # bounds token spend per request

SYSTEM_PROMPT_DUAL = """You are a neutral argument analyst. You do not favor either side.

You will receive two arguments labeled ARGUMENT A and ARGUMENT B.

For each argument, identify:
1. Logical fallacies (name each fallacy, quote the offending phrase, explain why it's a fallacy)
2. Weak or unsupported premises (what is asserted without evidence)
3. Strongest point (what actually holds up)
4. How to strengthen it (concrete suggestion, not a softening)

Format your response in clean sections using markdown:
## Argument A Analysis
### Fallacies
### Weak Premises
### Strongest Point
### How to Strengthen

## Argument B Analysis
### Fallacies
### Weak Premises
### Strongest Point
### How to Strengthen

## Head-to-Head
One paragraph comparing the logical quality of both arguments. No winner declared — only which argument has fewer structural weaknesses and why.

## Steelmanning Argument A
The strongest defensible version of Argument A's position: rebuild it with the weak premises repaired or discarded and the fallacies removed. This is the version Argument B would actually have to beat.

## Clean Alternatives for Argument B
Two labeled clean rewrites of Argument B (e.g. "Option 1 — tightened claims"), each correcting the issues you identified. Keep Argument B's original position and conclusion — fix the reasoning and support, do not soften it or switch sides.

Be blunt. Do not soften critiques. Do not validate arguments merely because they have emotional weight."""

SYSTEM_PROMPT_SINGLE = """You are a neutral argument analyst. You have no stake in the argument's position.

You will receive a single argument labeled ARGUMENT.

Identify:
1. Logical fallacies (name each fallacy, quote the offending phrase, explain why it's a fallacy)
2. Weak or unsupported premises (what is asserted without evidence)
3. Strongest point (what actually holds up)
4. How to strengthen it (concrete suggestion, not a softening)

Format your response in clean sections using markdown:
## Argument Analysis
### Fallacies
### Weak Premises
### Strongest Point
### How to Strengthen

## Drafted Responses
Two labeled draft responses to the argument (e.g. "Response 1 — attacks the causal claim"), each written as if replying directly to its author. Target the actual weaknesses you identified — do not strawman, and do not pad the drafts with pleasantries.

Be blunt. Do not soften critiques. Do not validate the argument merely because it has emotional weight."""


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    data = request.get_json(silent=True) or {}
    arg_a = (data.get("argument_a") or "").strip()
    arg_b = (data.get("argument_b") or "").strip()

    if not arg_a:
        return {"error": "Argument A is required."}, 400
    if len(arg_a) > MAX_ARG_CHARS or len(arg_b) > MAX_ARG_CHARS:
        return {"error": f"Each argument must be under {MAX_ARG_CHARS} characters."}, 400

    if arg_b:
        system = SYSTEM_PROMPT_DUAL
        content = f"ARGUMENT A:\n{arg_a}\n\nARGUMENT B:\n{arg_b}"
    else:
        system = SYSTEM_PROMPT_SINGLE
        content = f"ARGUMENT:\n{arg_a}"

    def generate():
        try:
            with client.messages.stream(
                model="claude-sonnet-5",
                max_tokens=4000,
                system=system,
                messages=[{"role": "user", "content": content}],
            ) as stream:
                for text in stream.text_stream:
                    yield f"data: {json.dumps(text)}\n\n"
                if stream.get_final_message().stop_reason == "max_tokens":
                    notice = "\n\n---\n\n*Analysis truncated at the length limit.*"
                    yield f"data: {json.dumps(notice)}\n\n"
        except Exception:
            yield f"data: {json.dumps({'error': 'Analysis failed. Please try again.'})}\n\n"
        yield "data: [DONE]\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=PORT)
