import os

from dotenv import load_dotenv
from groq import Groq

from db import get_connection

load_dotenv()
client = Groq(api_key=os.getenv("GROQ_API_KEY"))

MODEL = "openai/gpt-oss-20b"

SUMMARY_PROMPT = r"""You are a friendly tutor writing a study note for ONE topic of a
lecture. The note has two layers: what the slides say, then your own help to
understand it.

Layer 1, from the slides:
- Start with one sentence saying what the topic IS, before any details
- Then 3 to 5 short points, one idea each, each line starting with "- "
- Cite the page after each point, like [page 29]
- These points use only what the slides say, never outside knowledge

Layer 2, your help. After the points, a blank line, then these three lines,
each starting with its label exactly as written:
Intuition: 1 to 3 sentences in plain words: what the idea really means, why it
  matters, or a mental picture. Talk to the student, like a tutor would
Example: one small concrete example with actual numbers or a tiny case,
  worked out briefly
Watch out: the most common mistake or confusion with this topic
Layer 2 may use general knowledge, but must never contradict the slides, must
keep the slides' notation, and must not cite pages. Skip a line only if it would
be empty or forced.

Rules for both layers:
- Keep the course's own notation and terms
- If the slides use a specific convention (min-heap, 0-indexed, natural log),
  state that convention instead of offering both alternatives
- Mark key terms in bold with **double asterisks**, only where the term is
  defined or first matters, at most two per point

Math:
- Write every formula, variable with an index, or piece of notation as LaTeX
  inside \( and \), even a single symbol in running text: "for every \(\varepsilon > 0\)"
- Use real subscripts and superscripts: \(x_0\), \(x_k \to x_0\), \(\mathbb{R}^d\),
  \(\|v\|_p = \left(\sum_{i=1}^{d} |v_i|^p\right)^{1/p}\), \(\lim_{k \to \infty} f(x_k)\)
- Write set braces as \{ \}: \(\{x \in \mathbb{R}^d \mid \|x\|_p \le 1\}\)
- Logic: \(\neg\), \(\land\), \(\lor\), \(\to\), \(\leftrightarrow\), \(\models\), \(\equiv\),
  for example \(\varphi \models \psi\) and \(g \circ f\)
- Use $$ ... $$ only for a long formula on its own line, never \displaystyle inline
- Never put [page N] inside the math

The slide text comes from PDF extraction, so symbols may be broken. Write the
notation the slide means, not the broken characters:
- ↔ may appear as "→←", ⊨ as "|=", composition ∘ as "¶"
- subscripts may be flattened: "x0" usually means \(x_0\), "xk" means \(x_k\)

Format:
- No italics, no underscores around words, no numbered lists, no backticks
- No title, no heading, no closing summary, no other labels than the three above
- Answer in English
"""

class EmptySummaryError(Exception):
    pass


# Fixes for broken slide characters that the model may still copy outside the math.
SYMBOL_FIXES = {
    "‑": "-", "‐": "-", "→←": "↔", " |= ": " ⊨ ",
    # the model sometimes writes "[page 9]"; plain spaces keep page links working
    " ": " ", " ": " ",
}


def summarize_topic(topic_id: int) -> dict:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT title FROM topics WHERE id = %s", (topic_id,))
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"topic {topic_id} not found")
            title = row[0]

            cur.execute(
                "SELECT c.page_number, c.text FROM chunks c "
                "JOIN topic_chunks tc ON tc.chunk_id = c.id "
                "WHERE tc.topic_id = %s ORDER BY c.page_number",
                (topic_id,),
            )
            rows = cur.fetchall()

            if not rows:
                raise ValueError(f"topic {topic_id} has no slides")

            fragments = "\n\n".join(f"[page {page}]\n{text}" for page, text in rows)

            response = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": SUMMARY_PROMPT},
                    {
                        "role": "user",
                        "content": f"Topic: {title}\n\nSlides:\n\n{fragments}",
                    },
                ],
                temperature=0.2,
                max_completion_tokens=4096,
            )

            summary = (response.choices[0].message.content or "").strip()
            if not summary:
                # Happens when reasoning uses up the whole token budget.
                raise EmptySummaryError(f"model returned an empty summary for topic {topic_id}, try again")

            for broken, fixed in SYMBOL_FIXES.items():
                summary = summary.replace(broken, fixed)

            cur.execute(
                "UPDATE topics SET summary = %s, summary_origin = 'ai' "
                "WHERE id = %s AND summary_origin = 'ai'",
                (summary, topic_id),
            )
            written = cur.rowcount
            conn.commit()

    usage = response.usage
    return {
        "topic_id": topic_id,
        "title": title,
        "summary": summary,
        "saved": written == 1,
        "slides": len(rows),
        "tokens": f"{usage.prompt_tokens} / {usage.completion_tokens}",
    }