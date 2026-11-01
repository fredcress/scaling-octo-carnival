"""What you can change about the local model from Settings: the instructions (prompt)
for each kind of text, and whether the model thinks before answering.

Your versions live in the database; the built-in ones stay in coach.py and titles.py,
and "reset" just forgets yours.
"""
from .db import kv_delete, kv_get, kv_set

PROMPT_MIN, PROMPT_MAX = 20, 8000
# which switch each kind of text follows
THINK_GROUP = {"note": "coach", "review": "coach", "title": "titles"}


def defaults() -> dict:
    """name -> (label, built-in prompt), in the order Settings shows them."""
    from . import coach, titles  # here, not at the top: both of them import this module
    return {"note": ("Daily coach note", coach.SYSTEM_PROMPT),
            "review": ("Weekly review", coach.REVIEW_PROMPT),
            "title": ("Activity titles", titles.TITLE_PROMPT)}


def prompt(name: str, default: str) -> str:
    return kv_get(f"{name}_prompt") or default


def set_prompt(name: str, text: str | None) -> None:
    """Save your own prompt; None, empty or the built-in text puts the default back."""
    text = (text or "").strip()
    if not text or text == defaults()[name][1]:
        kv_delete(f"{name}_prompt")
    else:
        kv_set(f"{name}_prompt", text)


def think(group: str) -> bool:
    return bool((kv_get("ai_think") or {}).get(group, False))


def set_think(group: str, on: bool) -> None:
    kv_set("ai_think", {**(kv_get("ai_think") or {}), group: bool(on)})


def state() -> dict:
    return {
        "prompts": [{"name": n, "label": label, "prompt": prompt(n, d), "default": d,
                     "custom": kv_get(f"{n}_prompt") is not None, "think": think(THINK_GROUP[n])}
                    for n, (label, d) in defaults().items()],
        "think": {g: think(g) for g in ("coach", "titles")},
    }
