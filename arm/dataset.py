"""
arm/dataset.py — synthetic training data for the arm's Laya decision model.

Builds `{state, questions, expected}` rows (Laya's fine-tuning JSONL format) from
phrase banks in English, romanised Hinglish and Devanagari Hindi.

Decision: hold out whole phrasings, not just rows
  Each gesture's phrase bank is split once. Test rows use phrasings and
  templates the model never saw in training, so the reported accuracy measures
  generalisation to new wording — not memorisation of a template.

    python -m arm.dataset --out arm/data           # writes train.jsonl + test.jsonl
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from arm.schema import GESTURE_CRITERIA, QUESTIONS, SPEED_LEVELS

# Phrase banks: (english, hinglish, hindi). Each entry is a complete core phrase.
PHRASES: dict[str, dict[str, list[str]]] = {
    "wave": {
        "en": ["wave hello", "wave goodbye", "say hi", "greet everyone", "wave your hand", "say bye",
               "wave at the camera", "give a wave", "greet the guests", "wave to the team", "say hello to them",
               "wave bye bye"],
        "hi_latn": ["haath hilao", "hello bolo", "bye bolo", "sabko hi karo", "haath hila do", "tata karo",
                    "sabko greet karo", "hath hilao", "namaste nahi, hello bolo", "bye bye karo"],
        "hi_deva": ["हाथ हिलाओ", "हेलो बोलो", "बाय बोलो", "सबको हाय करो", "हाथ हिला दो", "टाटा करो", "सबका अभिवादन करो"],
    },
    "point": {
        "en": ["point left", "point to the right", "point at the screen", "point over there", "show me the door",
               "point at him", "indicate the left side", "point forward", "aim at the board", "point to that corner",
               "which way is it, point", "point at the window"],
        "hi_latn": ["udhar dikhao", "left mein dikhao", "right ki taraf ishara karo", "screen ki taraf point karo",
                    "us taraf dikhao", "idhar ishara karo", "darwaza dikhao", "saamne point karo", "baayein dikhao"],
        "hi_deva": ["उधर दिखाओ", "बाईं ओर इशारा करो", "दाईं तरफ दिखाओ", "स्क्रीन की तरफ इशारा करो", "उस तरफ इशारा करो", "सामने दिखाओ"],
    },
    "nod": {
        "en": ["nod yes", "say yes", "agree", "nod your head", "show you agree", "give a yes", "nod along",
               "yes, nod", "signal yes", "confirm with a nod"],
        "hi_latn": ["haan bolo", "haan mein sar hilao", "agree karo", "haan karo", "sahi hai bolo", "haan ka ishara karo",
                    "sir hilao haan"],
        "hi_deva": ["हाँ बोलो", "हाँ में सिर हिलाओ", "सहमति दिखाओ", "हाँ करो", "हाँ का इशारा करो"],
    },
    "shake_no": {
        "en": ["say no", "shake no", "disagree", "refuse", "shake your head", "signal no", "show you disagree",
               "no way, shake it", "deny it", "give a no"],
        "hi_latn": ["nahi bolo", "mana karo", "naa bolo", "nahi mein sar hilao", "disagree karo", "inkaar karo",
                    "na ka ishara karo"],
        "hi_deva": ["ना बोलो", "मना करो", "नहीं में सिर हिलाओ", "इनकार करो", "ना का इशारा करो"],
    },
    "bow": {
        "en": ["bow", "take a bow", "thank the audience", "say thank you", "show respect", "bow down",
               "thank everyone", "bow to the guests", "say thanks", "show gratitude"],
        "hi_latn": ["jhuko", "dhanyavaad bolo", "shukriya karo", "namaste karo", "sar jhukao", "thank you bolo",
                    "aadar dikhao", "jhuk jao"],
        "hi_deva": ["झुको", "धन्यवाद बोलो", "नमस्ते करो", "शुक्रिया अदा करो", "सिर झुकाओ", "आदर दिखाओ"],
    },
    "grab": {
        "en": ["pick up the pen", "grab it", "hold this", "take the cube", "close the claw", "grip the ball",
               "pick that up", "catch it", "lift the bottle cap", "grab the toy", "hold the marker"],
        "hi_latn": ["pen uthao", "isko pakdo", "ye pakad lo", "claw band karo", "ball uthao", "pakad ke rakho",
                    "cube utha lo", "isse pakdo"],
        "hi_deva": ["पेन उठाओ", "इसे पकड़ो", "यह पकड़ लो", "पंजा बंद करो", "गेंद उठाओ", "पकड़ कर रखो"],
    },
    "release": {
        "en": ["drop it", "let go", "release the pen", "put it down", "open the claw", "drop the ball",
               "release it", "let it fall", "set it down", "open your grip"],
        "hi_latn": ["chhod do", "neeche rakh do", "isko chhodo", "claw kholo", "gira do", "rakh do",
                    "pakad dheeli karo", "ball chhod do"],
        "hi_deva": ["छोड़ दो", "नीचे रख दो", "इसे छोड़ो", "पंजा खोलो", "गिरा दो", "रख दो"],
    },
    "dance": {
        "en": ["dance", "celebrate", "do a happy dance", "party time", "show some moves", "celebrate the launch",
               "do a victory dance", "get groovy", "boogie", "cheer for us"],
        "hi_latn": ["nacho", "nach ke dikhao", "jashn manao", "khushi manao", "thoda dance karo", "party karo",
                    "naach do", "celebrate karo"],
        "hi_deva": ["नाचो", "नाच के दिखाओ", "जश्न मनाओ", "खुशी मनाओ", "थोड़ा डांस करो", "पार्टी करो"],
    },
    "rest": {
        "en": ["stop", "relax", "go to rest", "go home", "stop moving", "freeze", "that's enough", "return to start",
               "calm down", "reset your position", "stand still"],
        "hi_latn": ["ruko", "bas karo", "aaram karo", "ruk jao", "wapas jao", "seedhe ho jao", "hilna band karo",
                    "home position mein jao"],
        "hi_deva": ["रुको", "बस करो", "आराम करो", "रुक जाओ", "वापस जाओ", "हिलना बंद करो"],
    },
    "random": {
        "en": ["do anything", "surprise me", "do something", "show me a trick", "do a random move", "pick any gesture",
               "do whatever you like", "entertain us", "show off", "do something cool"],
        "hi_latn": ["kuch bhi karo", "kuch karke dikhao", "koi bhi move karo", "surprise do", "kuch mazedaar karo",
                    "apni marzi se karo"],
        "hi_deva": ["कुछ भी करो", "कुछ करके दिखाओ", "कोई भी मूव करो", "कुछ मज़ेदार करो", "अपनी मर्ज़ी से करो"],
    },
    # Not an instruction to the arm: meeting talk, questions, chatter.
    "none": {
        "en": ["what's the weather today", "the meeting is at three", "can you send me the report",
               "I think the build is broken", "let's review the sprint", "who is joining the call",
               "the deadline moved to friday", "did you check the logs", "lunch is ready", "my laptop is slow",
               "we need more test coverage", "please share your screen", "the client approved it",
               "how was your weekend", "let me check the numbers", "the server is down again",
               "I will update the ticket", "good morning everyone", "is the mic working", "next slide please"],
        "hi_latn": ["meeting kal teen baje hai", "report bhej do", "build toot gaya hai", "kaun join kar raha hai",
                    "deadline friday ho gayi", "logs check kiye kya", "khana ready hai", "mera laptop slow hai",
                    "client ne approve kar diya", "weekend kaisa tha", "server phir se down hai",
                    "ticket update kar dunga", "sab log aa gaye kya", "agla slide dikhao na yaar",
                    "chai peene chalein", "kal ki meeting cancel hai"],
        "hi_deva": ["मीटिंग कल तीन बजे है", "रिपोर्ट भेज दो", "बिल्ड टूट गया है", "कौन जॉइन कर रहा है",
                    "डेडलाइन शुक्रवार हो गई", "खाना तैयार है", "मेरा लैपटॉप धीमा है", "क्लाइंट ने मंज़ूरी दे दी",
                    "सर्वर फिर से डाउन है", "चाय पीने चलें", "कल की मीटिंग रद्द है"],
    },
}

# Speed modifiers per level and language. Level 1 (normal) has no modifier.
SPEED_WORDS = {
    0: {"en": ["slowly", "gently", "very slowly", "take it slow"], "hi_latn": ["dheere", "aaram se", "dheere dheere"],
        "hi_deva": ["धीरे", "आराम से", "धीरे धीरे"]},
    2: {"en": ["quickly", "fast", "hurry", "right now, fast"], "hi_latn": ["jaldi", "jaldi se", "tezi se", "fatafat"],
        "hi_deva": ["जल्दी", "जल्दी से", "तेज़ी से", "फटाफट"]},
}

# Wrappers: how people address the arm. `{p}` is the core phrase.
TEMPLATES = {
    "en": ["{p}", "{p} please", "robot, {p}", "hey jarvis {p}", "can you {p}", "arm, {p} now", "okay {p}",
           "could you {p} for me", "{p}, robot", "jarvis, {p}"],
    "hi_latn": ["{p}", "{p} na", "robot {p}", "jarvis {p}", "zara {p}", "arm {p}", "{p} yaar", "please {p}",
                "accha {p}", "chalo {p}"],
    "hi_deva": ["{p}", "{p} ना", "रोबोट {p}", "जार्विस {p}", "ज़रा {p}", "कृपया {p}", "चलो {p}", "अच्छा {p}"],
}


def _split(items: list[str], rng: random.Random, test_frac: float) -> tuple[list[str], list[str]]:
    items = items[:]
    rng.shuffle(items)
    n_test = max(1, round(len(items) * test_frac))
    return items[n_test:], items[:n_test]


def _with_speed(phrase: str, level: int, lang: str, rng: random.Random) -> str:
    if level == 1:
        return phrase
    word = rng.choice(SPEED_WORDS[level][lang])
    return f"{word} {phrase}" if rng.random() < 0.5 else f"{phrase} {word}"


def build(seed: int = 7, per_phrase: int = 6, test_frac: float = 0.25) -> tuple[list[dict], list[dict]]:
    rng = random.Random(seed)
    tmpl_split = {lang: _split(t, rng, 0.2) for lang, t in TEMPLATES.items()}
    train, test = [], []
    for gesture, langs in PHRASES.items():
        for lang, phrases in langs.items():
            tr_p, te_p = _split(phrases, rng, test_frac)
            for split_rows, plist, templates in ((train, tr_p, tmpl_split[lang][0]),
                                                 (test, te_p, tmpl_split[lang][1] + tmpl_split[lang][0][:2])):
                for p in plist:
                    for _ in range(per_phrase if split_rows is train else 2):
                        level = 1 if gesture == "none" else rng.choices([0, 1, 2], weights=[1, 2, 1])[0]
                        text = rng.choice(templates).format(p=_with_speed(p, level, lang, rng))
                        split_rows.append({
                            "state": {"command": text},
                            "questions": QUESTIONS,
                            "expected": {"gesture": gesture, "speed": level},
                            "meta": {"lang": lang},
                        })
    # De-duplicate identical texts within each split; the same text never in both.
    seen_train = set()
    train_u = []
    for r in train:
        t = r["state"]["command"]
        if t not in seen_train:
            seen_train.add(t)
            train_u.append(r)
    test_u, seen_test = [], set()
    for r in test:
        t = r["state"]["command"]
        if t not in seen_train and t not in seen_test:
            seen_test.add(t)
            test_u.append(r)
    rng.shuffle(train_u)
    return train_u, test_u


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="arm/data")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    train, test = build(seed=args.seed)
    for name, rows in (("train", train), ("test", test)):
        with open(out / f"{name}.jsonl", "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"train={len(train)} test={len(test)} → {out}")


if __name__ == "__main__":
    main()
