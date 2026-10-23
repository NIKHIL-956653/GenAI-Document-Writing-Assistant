"""
Evaluation harness: score generated documents against their source ticket.

- faithfulness — no claims absent from the ticket (entity grounding; optional LLM judge)
- completeness — every required template section present and non-trivial
- structure    — the document follows the template's output schema

`docgen eval` runs the fixture tickets in fixtures/tickets.yaml through one
or more models and reports these per model, with tokens and cost.
"""
from docgen.eval.runner import (
                                EvalRecord,
                                EvalReport,
                                Fixture,
                                ModelSpec,
                                load_fixtures,
                                run_eval,
                                score_document,
)
from docgen.eval.scorers import Check, Score, completeness, faithfulness, structure

__all__ = ["Check", "EvalRecord", "EvalReport", "Fixture", "ModelSpec", "Score", "completeness",
           "faithfulness", "load_fixtures", "run_eval", "score_document", "structure"]
