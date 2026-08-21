"""Career Compass scoring -- a real, deterministic RIASEC (Holland Code)
interest-inventory scorer. No AI gateway call. See
`app/models/career_compass.py` for the full disclosed methodology
reasoning (why Option A -- deterministic -- was chosen over an
LLM-graded Option B).

The inventory: 10 questions, each offering exactly 6 options in a FIXED
order corresponding to the six RIASEC dimensions:

    index 0 = R (Realistic)     -- الواقعي
    index 1 = I (Investigative) -- البحثي
    index 2 = A (Artistic)      -- الفني
    index 3 = S (Social)        -- الاجتماعي
    index 4 = E (Enterprising)  -- الريادي
    index 5 = C (Conventional)  -- التنظيمي

A student answers by picking, for each question, which of the 6 option
"flavors" appeals to them most (submitted as the option's index 0-5).
Scoring is a straight tally: each answer adds one point to that
dimension's raw count. Over 10 questions the six raw counts always sum
to exactly 10. This is a real, standard interest-inventory scoring rule
(the same "count how often each dimension was chosen" logic behind
O*NET's own Interest Profiler) -- not a fabricated formula.

`match_percentage` is the top dimension's raw count expressed as a
percentage of the 10 questions (e.g. 4/10 -> 40.0). It is honestly a
measure of "how consistently you leaned toward this interest area
across 10 questions," not a validated probability of career success --
this is exactly the caveat threaded into the disclosure copy shipped in
quiz.html.

`matched_careers` maps from the single top-scoring RIASEC dimension (or
dimensions, if there is an exact tie for first) to a small, real,
defined list of career/major names -- deliberately not a full 15-entry
two-letter Holland Code cross table, which would be over-building for a
first real version of this feature with no user data yet to validate
which combinations actually get produced in practice.
"""

from __future__ import annotations

from dataclasses import dataclass

# Fixed RIASEC option order used by every question. Any code building or
# validating an answer set must agree with this order.
DIMENSIONS: tuple[str, ...] = ("R", "I", "A", "S", "E", "C")

DIMENSION_NAMES_BILINGUAL: dict[str, tuple[str, str]] = {
    "R": ("الواقعي", "Realistic"),
    "I": ("البحثي", "Investigative"),
    "A": ("الفني", "Artistic"),
    "S": ("الاجتماعي", "Social"),
    "E": ("الريادي", "Enterprising"),
    "C": ("التنظيمي", "Conventional"),
}

NUM_QUESTIONS = 10

# Real, defined dimension -> career/major cluster mapping (4 per
# dimension), bilingual. This is the actual mapping used by the scorer,
# not a placeholder.
CAREER_MAP: dict[str, list[dict[str, str]]] = {
    "R": [
        {"name_ar": "الهندسة الميكانيكية", "name_en": "Mechanical Engineering"},
        {"name_ar": "هندسة الطيران", "name_en": "Aerospace Engineering"},
        {"name_ar": "الروبوتات والتحكم الآلي", "name_en": "Robotics & Automation"},
        {"name_ar": "التقنية الصناعية والحرف الفنية", "name_en": "Industrial Technology & Skilled Trades"},
    ],
    "I": [
        {"name_ar": "هندسة البرمجيات", "name_en": "Software Engineering"},
        {"name_ar": "علم البيانات", "name_en": "Data Science"},
        {"name_ar": "الطب والبحث العلمي", "name_en": "Medicine & Scientific Research"},
        {"name_ar": "الفيزياء التطبيقية", "name_en": "Applied Physics"},
    ],
    "A": [
        {"name_ar": "التصميم الجرافيكي", "name_en": "Graphic Design"},
        {"name_ar": "العمارة", "name_en": "Architecture"},
        {"name_ar": "صناعة الأفلام والإعلام", "name_en": "Film & Media Production"},
        {"name_ar": "الكتابة الإبداعية", "name_en": "Creative Writing"},
    ],
    "S": [
        {"name_ar": "التربية والتعليم", "name_en": "Education & Teaching"},
        {"name_ar": "علم النفس", "name_en": "Psychology"},
        {"name_ar": "التمريض", "name_en": "Nursing"},
        {"name_ar": "العمل الاجتماعي", "name_en": "Social Work"},
    ],
    "E": [
        {"name_ar": "إدارة الأعمال", "name_en": "Business Administration"},
        {"name_ar": "ريادة الأعمال", "name_en": "Entrepreneurship"},
        {"name_ar": "التسويق", "name_en": "Marketing"},
        {"name_ar": "القانون", "name_en": "Law"},
    ],
    "C": [
        {"name_ar": "المحاسبة", "name_en": "Accounting"},
        {"name_ar": "التمويل", "name_en": "Finance"},
        {"name_ar": "تحليل البيانات الإدارية", "name_en": "Business/Data Analytics"},
        {"name_ar": "إدارة المشاريع", "name_en": "Project Management"},
    ],
}

# The real 10-question bank. Each question has exactly 6 options in the
# fixed R,I,A,S,E,C order. `id` is 1-indexed and matches submission order.
QUESTIONS: list[dict] = [
    {
        "id": 1,
        "text_ar": "أي نشاط تفضلينه أكثر؟",
        "text_en": "Which activity do you enjoy most?",
        "options": [
            {"text_ar": "إصلاح أو تركيب الأجهزة والآلات", "text_en": "Repairing or assembling machines"},
            {"text_ar": "حل الألغاز والمسائل المنطقية", "text_en": "Solving puzzles and logic problems"},
            {"text_ar": "الرسم والتصميم", "text_en": "Drawing and designing"},
            {"text_ar": "مساعدة الآخرين وتقديم الدعم", "text_en": "Helping others and offering support"},
            {"text_ar": "التحدث أمام الناس وتنظيم الفعاليات", "text_en": "Public speaking and organizing events"},
            {"text_ar": "تنظيم الملفات والبيانات بدقة", "text_en": "Organizing files and data precisely"},
        ],
    },
    {
        "id": 2,
        "text_ar": "في مشروع جماعي، أي دور تختارينه؟",
        "text_en": "In a group project, which role do you pick?",
        "options": [
            {"text_ar": "بناء أو تجميع نموذج فعلي", "text_en": "Building or assembling a physical model"},
            {"text_ar": "البحث وتحليل المعلومات", "text_en": "Researching and analyzing information"},
            {"text_ar": "تصميم العرض بشكل جذاب", "text_en": "Designing the presentation visually"},
            {"text_ar": "الحرص على مشاركة الجميع", "text_en": "Making sure everyone is included"},
            {"text_ar": "قيادة الفريق وتوزيع المهام", "text_en": "Leading the team and assigning tasks"},
            {"text_ar": "تنظيم الجدول الزمني ومتابعة التقدم", "text_en": "Organizing the timeline and tracking progress"},
        ],
    },
    {
        "id": 3,
        "text_ar": "ما هي مادتك الدراسية المفضلة؟",
        "text_en": "What's your favorite school subject?",
        "options": [
            {"text_ar": "الأعمال اليدوية والتقنية", "text_en": "Shop class / hands-on technical work"},
            {"text_ar": "الفيزياء والرياضيات", "text_en": "Physics and Math"},
            {"text_ar": "الفنون والتصميم", "text_en": "Art and Design"},
            {"text_ar": "علم النفس والتربية", "text_en": "Psychology and Education"},
            {"text_ar": "الاقتصاد وريادة الأعمال", "text_en": "Economics and Entrepreneurship"},
            {"text_ar": "المحاسبة وإدارة المكاتب", "text_en": "Accounting and Office Administration"},
        ],
    },
    {
        "id": 4,
        "text_ar": "ماذا تفضلين في وقت فراغك؟",
        "text_en": "What do you prefer to do in your free time?",
        "options": [
            {"text_ar": "إصلاح شيء معطّل في المنزل", "text_en": "Fix something broken at home"},
            {"text_ar": "القراءة عن اكتشاف علمي", "text_en": "Read about a scientific discovery"},
            {"text_ar": "كتابة قصة أو تأليف موسيقى", "text_en": "Write a story or compose music"},
            {"text_ar": "التطوع لخدمة المجتمع", "text_en": "Volunteer for the community"},
            {"text_ar": "التخطيط لفكرة مشروع صغير", "text_en": "Plan a small business idea"},
            {"text_ar": "تنظيم غرفتك أو جدولك اليومي", "text_en": "Organize your room or daily schedule"},
        ],
    },
    {
        "id": 5,
        "text_ar": "ما بيئة العمل المفضلة لديك؟",
        "text_en": "What's your preferred work environment?",
        "options": [
            {"text_ar": "ورشة عمل أو مختبر عملي", "text_en": "A hands-on workshop or lab"},
            {"text_ar": "مختبر بحث هادئ", "text_en": "A quiet research lab"},
            {"text_ar": "استوديو إبداعي", "text_en": "A creative studio"},
            {"text_ar": "مدرسة أو مركز مجتمعي", "text_en": "A school or community center"},
            {"text_ar": "شركة ناشئة سريعة الإيقاع", "text_en": "A fast-paced startup"},
            {"text_ar": "مكتب منظم بإجراءات واضحة", "text_en": "An organized office with clear procedures"},
        ],
    },
    {
        "id": 6,
        "text_ar": "كيف تحلين مشكلة صعبة؟",
        "text_en": "How do you solve a difficult problem?",
        "options": [
            {"text_ar": "بحل عملي بيدي مباشرة", "text_en": "A hands-on solution myself"},
            {"text_ar": "بتحليلها خطوة بخطوة بالبيانات", "text_en": "Analyzing it step-by-step with data"},
            {"text_ar": "بحل إبداعي غير تقليدي", "text_en": "An unconventional, creative solution"},
            {"text_ar": "بالنقاش مع الآخرين", "text_en": "Discussing it with others"},
            {"text_ar": "بقرار سريع وتحمّل مسؤوليته", "text_en": "A fast decision, owning the outcome"},
            {"text_ar": "باتباع إجراء واضح ومحدد", "text_en": "Following a clear, defined procedure"},
        ],
    },
    {
        "id": 7,
        "text_ar": "ما الإنجاز الأكثر رضا بالنسبة لك؟",
        "text_en": "What achievement feels most satisfying to you?",
        "options": [
            {"text_ar": "بناء أو إصلاح شيء من الصفر", "text_en": "Building or fixing something from scratch"},
            {"text_ar": "اكتشاف إجابة لسؤال معقّد", "text_en": "Discovering the answer to a complex question"},
            {"text_ar": "إنتاج عمل إبداعي أصيل", "text_en": "Producing an original creative work"},
            {"text_ar": "مساعدة شخص في مشكلة حقيقية", "text_en": "Helping someone with a real problem"},
            {"text_ar": "قيادة مشروع إلى النجاح", "text_en": "Leading a project to success"},
            {"text_ar": "إنجاز مهمة معقدة بدقة تامة", "text_en": "Completing a complex task with total accuracy"},
        ],
    },
    {
        "id": 8,
        "text_ar": "أي نوع كتب أو فيديوهات تفضلين؟",
        "text_en": "What kind of books or videos do you prefer?",
        "options": [
            {"text_ar": "كيف تُصنع الأشياء (DIY)", "text_en": "How things are made (DIY)"},
            {"text_ar": "وثائقيات علمية", "text_en": "Science documentaries"},
            {"text_ar": "أفلام أو أعمال فنية", "text_en": "Films or creative art"},
            {"text_ar": "قصص ملهمة عن مساعدة الآخرين", "text_en": "Inspiring stories about helping others"},
            {"text_ar": "قصص نجاح رواد أعمال", "text_en": "Entrepreneur success stories"},
            {"text_ar": "أدلة تنظيم الوقت والإنتاجية", "text_en": "Time-management / productivity guides"},
        ],
    },
    {
        "id": 9,
        "text_ar": "كيف يصفك أصدقاؤك؟",
        "text_en": "How would your friends describe you?",
        "options": [
            {"text_ar": "عملي/ة ومحب/ة للعمل اليدوي", "text_en": "Practical and hands-on"},
            {"text_ar": "فضولي/ة ويطرح/تطرح الأسئلة", "text_en": "Curious, always asking questions"},
            {"text_ar": "خيالي/ة ومبدع/ة", "text_en": "Imaginative and creative"},
            {"text_ar": "متعاطف/ة ومستمع/ة جيد/ة", "text_en": "Empathetic and a good listener"},
            {"text_ar": "واثق/ة ومقنع/ة", "text_en": "Confident and persuasive"},
            {"text_ar": "منظم/ة ودقيق/ة", "text_en": "Organized and precise"},
        ],
    },
    {
        "id": 10,
        "text_ar": "لو أضفت مادة اختيارية، ماذا تختارين؟",
        "text_en": "If you added an elective, what would you choose?",
        "options": [
            {"text_ar": "الروبوتات والميكانيكا", "text_en": "Robotics and mechanics"},
            {"text_ar": "علوم الحاسب أو الأحياء", "text_en": "Computer science or biology"},
            {"text_ar": "التصميم الجرافيكي أو الموسيقى", "text_en": "Graphic design or music"},
            {"text_ar": "التربية أو علم الاجتماع", "text_en": "Education or sociology"},
            {"text_ar": "ريادة الأعمال", "text_en": "Entrepreneurship"},
            {"text_ar": "المحاسبة وإدارة المشاريع", "text_en": "Accounting and project management"},
        ],
    },
]


@dataclass(frozen=True)
class RiasecResult:
    dimension_scores: dict[str, int]
    holland_code: str
    match_percentage: float
    matched_careers: list[dict[str, str]]
    top_dimensions: tuple[str, ...]


class InvalidAnswersError(ValueError):
    """Raised when a submitted answer set isn't exactly 10 ints in 0-5."""


def score_riasec(answers: list[int]) -> RiasecResult:
    """Deterministically score a completed RIASEC answer set.

    `answers` must be exactly `NUM_QUESTIONS` ints, each 0-5 (an index
    into `DIMENSIONS` / a question's fixed option order). Raises
    `InvalidAnswersError` on any malformed input -- this function never
    guesses or silently drops bad data.
    """
    if not isinstance(answers, list) or len(answers) != NUM_QUESTIONS:
        raise InvalidAnswersError(f"Expected exactly {NUM_QUESTIONS} answers, got {len(answers) if isinstance(answers, list) else type(answers).__name__}")
    for a in answers:
        if not isinstance(a, int) or isinstance(a, bool) or a < 0 or a > 5:
            raise InvalidAnswersError(f"Each answer must be an int 0-5, got {a!r}")

    scores: dict[str, int] = {d: 0 for d in DIMENSIONS}
    for a in answers:
        scores[DIMENSIONS[a]] += 1

    top_score = max(scores.values())
    top_dimensions = tuple(d for d in DIMENSIONS if scores[d] == top_score)

    # Holland code: top 3 by score desc, ties broken by fixed dimension
    # order -- a stable sort on the fixed-order list does exactly this.
    ranked = sorted(DIMENSIONS, key=lambda d: scores[d], reverse=True)
    holland_code = "".join(ranked[:3])

    match_percentage = round((top_score / NUM_QUESTIONS) * 100, 1)

    matched_careers: list[dict[str, str]] = []
    for d in top_dimensions:
        matched_careers.extend(CAREER_MAP[d])

    return RiasecResult(
        dimension_scores=scores,
        holland_code=holland_code,
        match_percentage=match_percentage,
        matched_careers=matched_careers,
        top_dimensions=top_dimensions,
    )


def questions_public() -> list[dict]:
    """The question bank shaped for the API response (no scoring internals)."""
    return [
        {
            "id": q["id"],
            "text_ar": q["text_ar"],
            "text_en": q["text_en"],
            "options": [
                {"text_ar": o["text_ar"], "text_en": o["text_en"]} for o in q["options"]
            ],
        }
        for q in QUESTIONS
    ]
