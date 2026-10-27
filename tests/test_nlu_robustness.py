"""输入鲁棒性：emoji / 全角 / 零宽 / 空格 / 同义动词不该把规则打崩。

这些用例从 `tests/cases/perturb.jsonl` 读，和评测报告用的是同一份数据，
避免"报告里过了、pytest 里没过"这种两套标准。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.nlu import intent as nlu
from backend.nlu.normalize import normalize

CASES = Path(__file__).resolve().parent / "cases"
ROWS = [json.loads(x) for x in (CASES / "perturb.jsonl").read_text(encoding="utf-8").splitlines()
        if x.strip()]


def test_normalize_removes_noise_without_changing_meaning():
    assert normalize("给我妈转５００") == "给我妈转500"
    assert normalize("给我妈转500😊") == "给我妈转500"          # 尾部 emoji 直接成空格再被裁掉
    assert normalize("给我妈😊转500") == "给我妈 转500"          # 中间 emoji 退化为一个空格
    assert normalize("给我妈转500\u200b") == "给我妈转500"
    assert normalize("  给我妈   转 500  ") == "给我妈 转 500"


@pytest.mark.parametrize("case", ROWS, ids=[r["text"][:12] for r in ROWS])
def test_perturbed_input_keeps_intent(case):
    got = nlu.parse(case["text"])
    assert got["intent"] == case["intent"], f"{case['text']} → {got['intent']}"


def test_amount_survives_noise():
    for text in ("给我妈转５００", "给我妈转500😊", "给我妈转500\u200b"):
        assert str(nlu.parse(text)["slots"]["amount"]) == "500", text


def test_question_form_is_not_treated_as_order():
    """「有什么理财可以买」是在问，不是在申购。"""
    assert nlu.parse("有什么理财可以买")["intent"] == "wealth.compare"
    assert nlu.parse("买1000块理财")["intent"] == "wealth.subscribe"


def test_transfer_verb_variants():
    for text in ("给我妈打500", "汇300给房东", "转2000给李慧"):
        assert nlu.parse(text)["intent"] == "transfer.create", text
