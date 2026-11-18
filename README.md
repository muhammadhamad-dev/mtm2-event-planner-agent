# ConstraintOS

ConstraintOS reads event messages with Groq and uses Python to build a venue, schedule, meal, judging and budget plan. The source ledger records exact quotes, source dates, corrections, proposals and rejections.

## Run locally

Use Python 3.11 or later. Install in a virtual environment with `python -m pip install -e .`. Make `GROQ_API_KEY` available to the Python process from a confirmed free-plan Groq account.

On Windows, a user-level environment variable named `GROQ_API_KEY` also works. Set it in the Windows Environment Variables dialog and keep the value out of the repository and message files.

```text
python -m constraintos --check-provider
python -m constraintos path/to/messages.txt
python -m constraintos path/to/next-message.txt
python -m streamlit run app.py
```

Run from the project directory. The same file command reads the initial pack and each new message. The UI can open the existing state or process another text file using that same engine. Keep the original English and Roman Urdu text in UTF-8.

State, plan and trace live in the ignored `.constraintos/` directory. Submit `plan.json` and `trace.json`. To start a separate event, move the existing state directory aside first.

## Predicting model calls

Every fresh input file sent to the provider uses one call tagged `reading`. Duplicate files already processed from the same state use zero calls. Python records which plan areas the extracted changes affect and checks the resulting candidate for feasibility. Infeasible updates are rejected transactionally.

The provider trace shows request and response data, model, latency, rate-limit headers, and token usage as reported by Groq. Failed calls can lack usage. No fallback runs automatically.

Run the deterministic regression suite with `python -m unittest discover -s tests -v`. The provider check above verifies live access separately.
