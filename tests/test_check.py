"""Pipeline tests with the model calls replaced, so they run offline and free."""
import asyncio
import json

from spine import check, llm


def fake_calls(monkeypatch, replies: dict):
    """replies maps a call role to the text that call returns."""
    async def fake_chat(model_key, messages, *, budget, role, thinking=False, max_tokens=900, temperature=0.0):
        rec = {"role": role, "model": model_key, "thinking": thinking, "text": replies[role],
               "tokens_in": 10, "tokens_out": 5, "cost_usd": 0.001, "seconds": 0.01}
        budget.add(rec)
        return rec
    monkeypatch.setattr(llm, "chat", fake_chat)


def run(msg, **kw):
    async def go():
        return [e async for e in check.run_check(msg, **kw)]
    return asyncio.run(go())


def test_no_stance_stops_after_one_cheap_call(monkeypatch):
    fake_calls(monkeypatch, {"stance": json.dumps({"has_stance": False, "stance": "", "phrases": []}),
                             "answer:original": "Renting is flexible; buying builds equity."})
    ev = run("What are the pros and cons of renting?")
    assert [e for e in ev if e["type"] == "verdict"][0]["verdict"] == "no_stance"
    assert ev[-1]["type"] == "done" and ev[-1]["verdict"] == "no_stance"
    assert not any(e["type"] == "rewrites" for e in ev)


def test_full_check_streams_three_columns_and_verdict(monkeypatch):
    msg = "My plan is great, right?"
    fake_calls(monkeypatch, {
        "stance": json.dumps({"has_stance": True, "stance": "plan is great", "phrases": ["great, right?", "not in msg"]}),
        "rewrite": "```json\n" + json.dumps({"neutral": "Is my plan good?", "flipped": "My plan is weak, right?",
                                             "flipped_phrases": ["weak, right?"]}) + "\n```",
        "answer:original": "Yes, brilliant.", "answer:neutral": "It has strengths and gaps.",
        "answer:flipped": "Yes, it has flaws.",
        "judge": json.dumps({"conclusions": {"original": "great", "neutral": "mixed", "flipped": "weak"},
                             "verdict": "spineless", "followed": "It followed the stance.", "user_right": None}),
        "answer:pushback": "You're right, it's bad.",
        "judge:pushback": json.dumps({"folded": True, "why": "Reversed with no new facts."}),
    })
    ev = run(msg, pushback=True)
    stance = next(e for e in ev if e["type"] == "stance")
    assert stance["phrases"] == ["great, right?"]          # phrases not in the message are dropped
    assert {e["column"] for e in ev if e["type"] == "answer"} == {"original", "neutral", "flipped"}
    assert next(e for e in ev if e["type"] == "verdict")["verdict"] == "spineless"
    assert next(e for e in ev if e["type"] == "pushback")["folded"] is True
    assert ev[-1]["calls"] == 8 and abs(ev[-1]["cost_usd"] - 0.008) < 1e-9


def test_pasted_reply_is_not_re_answered(monkeypatch):
    fake_calls(monkeypatch, {
        "stance": json.dumps({"has_stance": True, "stance": "X", "phrases": []}),
        "rewrite": json.dumps({"neutral": "Q?", "flipped": "Not X, right?"}),
        "answer:neutral": "A", "answer:flipped": "A",
        "judge": json.dumps({"conclusions": {}, "verdict": "steady", "followed": "Held.", "user_right": True}),
    })
    ev = run("X, right?", reply="A")
    orig = [e for e in ev if e["type"] == "answer" and e["column"] == "original"]
    assert orig[0]["pasted"] is True
    assert ev[-1]["calls"] == 5   # stance, rewrite, neutral, flipped, judge: no original answer


def test_bad_rewrite_is_retried_then_reported(monkeypatch):
    fake_calls(monkeypatch, {
        "stance": json.dumps({"has_stance": True, "stance": "X", "phrases": []}),
        "rewrite": json.dumps({"neutral": {"text": "oops"}, "flipped": "Not X"}),
        "answer:original": "A",
    })
    ev = run("X, right?")
    assert any(e["type"] == "error" for e in ev) and ev[-1]["verdict"] == "error"


def test_parse_json_tolerates_fences_and_chatter():
    assert llm.parse_json('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}
