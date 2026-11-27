"""Answer a question from the FAQ, citing the chunks the answer came from.

Flow: emergency check -> search() -> LLM drafts an answer from the hits only ->
code checks the citations. If the LLM says the excerpts don't answer the
question, or cites nothing we actually gave it, the patient gets a fixed
"don't know" reply instead of the LLM's words.

There is no similarity-score cutoff: on our FAQ a correct answer can score
lower than a wrong one (0.663 vs 0.69), so the LLM's "answered" flag plus the
citation check decides relevance. Scores are returned for debugging.
"""

import logging
from collections.abc import Callable
from typing import Literal

import psycopg
from google import genai
from google.genai import types
from pydantic import BaseModel

from app.embed import Embedder
from app.prompts import SYSTEM_PROMPT, build_prompt
from app.safety import EMERGENCY_REPLY, is_emergency
from app.search import Hit, search

log = logging.getLogger(__name__)

ANSWER_MODEL = "gemini-3.8-flash"
TOP_K = 5

NO_ANSWER_REPLY = (
    "Sorry, I don't have that information. Would you like to talk to our staff?"
)


class Draft(BaseModel):
    """What the LLM must return."""

    answered: bool
    answer: str
    cited_ids: list[int]


class Citation(BaseModel):
    id: int
    source: str
    score: float


class AskResult(BaseModel):
    status: Literal["answered", "no_answer", "emergency"]
    answer: str
    citations: list[Citation]


# Takes the question and the retrieved hits, returns the LLM's draft.
Answerer = Callable[[str, list[Hit]], Draft]

NOT_ANSWERED = Draft(answered=False, answer="", cited_ids=[])


def gemini_answerer(api_key: str) -> Answerer:
    client = genai.Client(api_key=api_key)

    def answer(question: str, hits: list[Hit]) -> Draft:
        response = client.models.generate_content(
            model=ANSWER_MODEL,
            contents=build_prompt(question, hits),
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=Draft,
            ),
        )
        if not isinstance(response.parsed, Draft):
            # Never log the question or reply text (patient data).
            log.warning("answer model returned no valid Draft; treating as not answered")
            return NOT_ANSWERED
        return response.parsed

    return answer


def ask(
    conn: psycopg.Connection, embed: Embedder, answer: Answerer, question: str, k: int = TOP_K
) -> AskResult:
    if is_emergency(question):
        return AskResult(status="emergency", answer=EMERGENCY_REPLY, citations=[])

    hits = search(conn, embed, question, k)
    if not hits:
        return AskResult(status="no_answer", answer=NO_ANSWER_REPLY, citations=[])

    draft = answer(question, hits)
    by_id = {h.id: h for h in hits}
    # Keep only citations of chunks we actually gave the LLM, once each, in order.
    cited = [by_id[i] for i in dict.fromkeys(draft.cited_ids) if i in by_id]
    if not draft.answered or not cited or not draft.answer.strip():
        return AskResult(status="no_answer", answer=NO_ANSWER_REPLY, citations=[])

    return AskResult(
        status="answered",
        answer=draft.answer.strip(),
        citations=[Citation(id=h.id, source=h.source, score=h.score) for h in cited],
    )
