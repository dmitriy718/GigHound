"""Anti-detection text engine.

Post-processes generated proposal text so it doesn't trip platform AI
detection (linguistic patterns, structural fingerprints):

  * opening rotation from a 50+ pool stored in Redis (never reuse recent ones)
  * banned openers/phrases stripped ("I hope this finds you well", ...)
  * AI-tell removal: furthermore/moreover/additionally chains, numbered lists
  * sentence-length distribution report (target 40% short / 40% mid / 20% long)
  * personality marker injection (casual transitions)
  * humanization plan: 1-2 realistic typo→correction ops per 200 words, for
    the stealth browser's typing simulation (raw text + plan are stored;
    the final rendered text is unchanged)
"""
import hashlib
import logging
import random
import re

from .cache import cache

log = logging.getLogger(__name__)

BANNED_PHRASES = [
    "i hope this finds you well", "i hope this message finds you well",
    "i am excited", "i'm excited to", "dear hiring manager", "dear sir",
    "to whom it may concern", "i am writing to express",
    "look no further", "your search ends here", "i am the perfect fit",
]

AI_TELL_WORDS = ["furthermore", "moreover", "additionally", "in addition",
                 "firstly", "secondly", "thirdly", "in conclusion"]

PERSONALITY_MARKERS = [
    "Honestly,", "Here's the thing —", "Quick thought:", "Real talk:",
    "Worth mentioning:", "Funny enough,", "Small detail, but",
]

OPENINGS_POOL = [
    "Your {tech} project caught my eye —",
    "Saw your post about {title} and had a few ideas.",
    "{title} looks like a fun build.",
    "I just finished something similar to {title}.",
    "This is right in my wheelhouse — {tech} is what I do daily.",
    "Read through your brief twice. Solid spec.",
    "You had me at {tech}.",
    "Not going to waste your time with fluff.",
    "Quick question before anything else —",
    "I've shipped three {tech} projects this year alone.",
    "Your timeline looks tight but doable.",
    "Most bids you'll get will be copy-paste. This isn't one.",
    "I noticed something in your description others might miss.",
    "{title} — I can start this week.",
    "Been doing {tech} work for years; this one's straightforward.",
    "Your budget actually makes sense for once. Refreshing.",
    "I build exactly this kind of thing.",
    "Skipped the generic intro. Here's my plan.",
    "Two things stood out in your post.",
    "This reminds me of a project I wrapped last month.",
    "I've got a working approach for {title} already.",
    "Short version: yes, I can do this, and here's how.",
    "Your description is refreshingly specific.",
    "I don't bid on many jobs. This one earned it.",
    "The {tech} part is the easy bit — here's the real challenge.",
    "Read your brief. No red flags. Let's talk details.",
    "I've solved this exact problem before.",
    "You're asking for {tech} done right — that's my entire portfolio.",
    "Before the pitch: one thing worth clarifying.",
    "I could write a wall of text. Instead, three short paragraphs.",
    "This scope is realistic. Here's my read on it.",
    "Happy to see a brief with actual deliverables listed.",
    "I skimmed ten other posts today. Yours got a full read.",
    "My last {tech} client left a 5-star review for similar work.",
    "Straight to it — I can deliver {title}.",
    "Your project ticks every box on my checklist.",
    "I keep a short client list so projects like yours get real attention.",
    "No templates here. I wrote this after reading your full brief.",
    "The trick with {tech} projects is the details — yours are clear.",
    "I had to double-check your budget. It's fair, and I can work with it.",
    "Something in your description tells me you've been burned before.",
    "Let me skip the sales pitch and talk approach.",
    "Your job post answers most of my questions already.",
    "I do my best work on projects exactly like this.",
    "Here's my honest take on {title}.",
    "This is a one-developer job, and I'd be that developer.",
    "I'll be upfront about what's easy and what's tricky here.",
    "Your {tech} requirements are specific enough that I can quote confidently.",
    "Timing works — I have capacity opening this week.",
    "I read the whole post, including the part most bidders skip.",
]

_TYPO_MAP = {"the": "teh", "and": "adn", "you": "yuo", "with": "wiht",
             "that": "taht", "for": "fro", "your": "yuor", "this": "tihs",
             "have": "hvae", "from": "form", "project": "porject", "would": "woudl"}

_redis_openings_key = "antidetect:openings_pool"
_redis_used_key = "antidetect:openings_used"


def seed_openings_pool():
    """Idempotently load the openings pool into Redis (or no-op offline)."""
    if cache._r is None:
        return
    if not cache._r.exists(_redis_openings_key):
        cache._r.sadd(_redis_openings_key, *OPENINGS_POOL)


def pick_opening(title: str = "", tech: str = "") -> str:
    """Pick an opening, preferring Redis rotation (no repeats until pool exhausts)."""
    opening = None
    if cache._r is not None:
        seed_openings_pool()
        opening = cache._r.spop(_redis_openings_key)
        if opening is None:  # pool exhausted → reshuffle
            cache._r.delete(_redis_used_key)
            cache._r.sadd(_redis_openings_key, *OPENINGS_POOL)
            opening = cache._r.spop(_redis_openings_key)
        if opening:
            cache._r.sadd(_redis_used_key, opening)
    if opening is None:
        opening = random.choice(OPENINGS_POOL)
    return opening.replace("{title}", (title or "your project")[:60]).replace(
        "{tech}", tech or "this")


def sentence_stats(text: str) -> dict:
    """Sentence-length distribution: short <8 words, long >20 words."""
    sentences = [s.strip() for s in re.split(r"[.!?]+\s*", text) if s.strip()]
    if not sentences:
        return {"total": 0, "short_pct": 0, "medium_pct": 0, "long_pct": 0}
    counts = {"short": 0, "medium": 0, "long": 0}
    for s in sentences:
        n = len(s.split())
        counts["short" if n < 8 else "long" if n > 20 else "medium"] += 1
    total = len(sentences)
    return {
        "total": total,
        "short_pct": round(100 * counts["short"] / total),
        "medium_pct": round(100 * counts["medium"] / total),
        "long_pct": round(100 * counts["long"] / total),
    }


def strip_ai_tells(text: str, platform: str = "") -> str:
    """Remove banned phrases, AI-tell connectors, and list formatting."""
    out = text
    for phrase in BANNED_PHRASES:
        out = re.sub(re.escape(phrase), "", out, flags=re.IGNORECASE)
    # AI-tell words: keep at most one, drop subsequent chains
    seen = False
    for word in AI_TELL_WORDS:
        def _sub(m, w=word):
            nonlocal seen
            if seen:
                return ""
            seen = True
            return m.group(0)
        out = re.sub(rf"\b{re.escape(word)}\b[:,]?", _sub, out, flags=re.IGNORECASE)
    # no numbered/bulleted lists in proposals (structural fingerprint)
    out = re.sub(r"(?:^|(?<=[.!?]\s))\s*(?:\d+[.)]|[-•*])\s+", "", out, flags=re.MULTILINE)
    # collapse artifacts left by removals
    out = re.sub(r",\s*\.", ".", out)
    out = re.sub(r"^[,\s]+", "", out, flags=re.MULTILINE)
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = re.sub(r" {2,}", " ", out)
    return out.strip()


def inject_personality(text: str, max_markers: int = 1) -> str:
    """Splice a casual transition before a mid-text sentence."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    if len(sentences) < 3 or max_markers < 1:
        return text
    idx = random.randint(1, len(sentences) - 2)
    sentences[idx] = f"{random.choice(PERSONALITY_MARKERS)} {sentences[idx][0].lower()}{sentences[idx][1:]}"
    return " ".join(sentences)


def build_typing_plan(text: str, seed: int | None = None) -> list[dict]:
    """1-2 typo→backspace→correction ops per 200 words for typing simulation.

    Each op: {"word": final word, "typo": mistyped form, "word_index": n}
    The stealth worker types `typo`, backspaces len(typo), then types `word`.
    """
    rng = random.Random(seed if seed is not None else hashlib.md5(text.encode()).hexdigest())
    words = text.split()
    n_typos = max(1, min(3, len(words) // 200 + 1))
    candidates = [
        (i, w) for i, w in enumerate(words)
        if w.strip(".,;:!?").lower() in _TYPO_MAP
    ]
    rng.shuffle(candidates)
    plan = []
    for i, w in candidates[:n_typos]:
        clean = w.strip(".,;:!?")
        plan.append({"word": clean, "typo": _TYPO_MAP[clean.lower()], "word_index": i})
    return plan


def humanize(text: str, platform: str = "", title: str = "", tech: str = "") -> dict:
    """Full anti-detection pass. Returns raw/humanized/plan/stats."""
    cleaned = strip_ai_tells(text, platform)
    cleaned = inject_personality(cleaned)
    return {
        "raw_text": text,
        "humanized_text": cleaned,
        "typing_plan": build_typing_plan(cleaned),
        "sentence_stats": sentence_stats(cleaned),
        "opening_suggestion": pick_opening(title=title, tech=tech),
    }
