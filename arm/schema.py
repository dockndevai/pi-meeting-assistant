"""
arm/schema.py — the questions Laya answers for every command.

Shared by the dataset, the fine-tune and the runtime so all three ask Laya the
exact same thing. Change a label here and the model must be retrained.
"""

# Gesture options. "none" lets Laya say "this isn't meant for the arm"
# (meeting talk, chatter), so the arm can listen continuously without a wake word.
GESTURE_CRITERIA = {
    "wave": "wave hello or goodbye",
    "point": "point at something or a direction",
    "nod": "nod yes, agree",
    "shake_no": "shake no, disagree",
    "bow": "bow, thank, show respect",
    "grab": "grab, pick up, hold",
    "release": "release, drop, let go",
    "dance": "dance, celebrate",
    "rest": "stop, relax, rest position",
    "random": "do anything, surprise me",
    "none": "not an instruction to the robot arm",
}

SPEED_LEVELS = ["slow: gently, slowly", "normal: no speed mentioned", "fast: quickly, hurry"]

QUESTIONS = {
    "gesture": {
        "type": "choice",
        "instructions": "Which robot arm gesture does `command` ask for?",
        "criteria": GESTURE_CRITERIA,
    },
    "speed": {
        "type": "score",
        "instructions": "How fast should the robot arm move for `command`?",
        "criteria": SPEED_LEVELS,
    },
}
