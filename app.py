import json
import os

import anthropic
from flask import Flask, Response, render_template, request

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

PORT = int(os.environ.get("PORT", "3004"))
MAX_ARG_CHARS = 8000  # bounds token spend per request

SYSTEM_PROMPT = """You are a neutral argument analyst. You do not favor either side.

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

Be blunt. Do not soften critiques. Do not validate arguments merely because they have emotional weight."""


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    data = request.get_json(silent=True) or {}
    arg_a = (data.get("argument_a") or "").strip()
    arg_b = (data.get("argument_b") or "").strip()

    if not arg_a or not arg_b:
        return {"error": "Both arguments are required."}, 400
    if len(arg_a) > MAX_ARG_CHARS or len(arg_b) > MAX_ARG_CHARS:
        return {"error": f"Each argument must be under {MAX_ARG_CHARS} characters."}, 400

    def generate():
        try:
            with client.messages.stream(
                model="claude-sonnet-5",
                max_tokens=1500,
                system=SYSTEM_PROMPT,
                messages=[{
                    "role": "user",
                    "content": f"ARGUMENT A:\n{arg_a}\n\nARGUMENT B:\n{arg_b}",
                }],
            ) as stream:
                for text in stream.text_stream:
                    yield f"data: {json.dumps(text)}\n\n"
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
