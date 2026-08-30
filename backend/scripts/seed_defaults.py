"""Seed one sensible default per dashboard collection for the demo user.

All rows are tenant-owned (AD-1), so seeding happens under a demo account:
    email: demo@gighound.local   password: demo1234

Idempotent: a default is only created when the demo user's collection is
completely empty, so running this repeatedly (or against a populated DB)
never duplicates or overwrites user data. Wired defaults reference the
demo user's first existing keyword group / filter when present.

Run:  .venv/bin/python -m scripts.seed_defaults   (from backend/)
      python scripts/seed_defaults.py             (also works — path self-fixes)
"""

import sys
from pathlib import Path

# allow running as a plain script (incl. inside Docker) without PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth import hash_password
from app.database import SessionLocal
from app import models as m

DEMO_EMAIL = "demo@gighound.local"
DEMO_PASSWORD = "demo1234"


def get_or_create_demo_user(db) -> m.User:
    user = db.query(m.User).filter(m.User.email == DEMO_EMAIL).first()
    if not user:
        user = m.User(email=DEMO_EMAIL, password_hash=hash_password(DEMO_PASSWORD),
                      display_name="Demo User")
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def seed(db) -> list[str]:
    created: list[str] = []
    user = get_or_create_demo_user(db)
    uid = user.id

    if db.query(m.KeywordGroup).filter(m.KeywordGroup.user_id == uid).count() == 0:
        group = m.KeywordGroup(user_id=uid, name="Default — Full-Stack Web",
                               service_type="web-development")
        group.keywords = [
            m.Keyword(term="react", kind="primary", weight=0.9),
            m.Keyword(term="typescript", kind="primary", weight=0.85),
            m.Keyword(term="node.js", kind="primary", weight=0.8),
            m.Keyword(term="next.js", kind="primary", weight=0.75),
            m.Keyword(term="graphql", kind="secondary", weight=0.0),
            m.Keyword(term="postgresql", kind="secondary", weight=0.0),
            m.Keyword(term="tailwind", kind="secondary", weight=0.0),
            m.Keyword(term="wordpress", kind="negative", weight=0.0),
            m.Keyword(term="php", kind="negative", weight=0.0),
        ]
        db.add(group)
        created.append("keyword group")

    if db.query(m.SearchFilter).filter(m.SearchFilter.user_id == uid).count() == 0:
        db.flush()
        group = (db.query(m.KeywordGroup).filter(m.KeywordGroup.user_id == uid)
                 .order_by(m.KeywordGroup.id).first())
        db.add(
            m.SearchFilter(
                user_id=uid,
                name="Default — Remote web jobs",
                keyword_group_id=group.id if group else None,
                platforms=["upwork", "freelancer", "linkedin", "indeed"],
                job_types=["fixed", "hourly"],
                budgets=[{"platform": "upwork", "min": 500, "max": None, "currency": "USD"}],
                experience_levels=["intermediate", "expert"],
                client_filters={"payment_verified": True},
                posted_within_hours=48,
                apply_deadline_within_hours=None,
                work_arrangements=["remote"],
                languages=["English"],
                max_proposals=20,
                quality_threshold=40.0,
            )
        )
        created.append("search filter")

    if db.query(m.SearchProfile).filter(m.SearchProfile.user_id == uid).count() == 0:
        db.flush()
        group = (db.query(m.KeywordGroup).filter(m.KeywordGroup.user_id == uid)
                 .order_by(m.KeywordGroup.id).first())
        filt = (db.query(m.SearchFilter).filter(m.SearchFilter.user_id == uid)
                .order_by(m.SearchFilter.id).first())
        db.add(
            m.SearchProfile(
                user_id=uid,
                name="Default — Full-Stack Web",
                keyword_group_id=group.id if group else None,
                filter_id=filt.id if filt else None,
                boolean_query="(React OR Next.js) AND (NOT WordPress)",
                auto_queue_proposals=True,
            )
        )
        created.append("search profile")

    # Per-platform pitch defaults: each platform has its own culture, so a
    # single generic template leaves most platform tabs blank.
    DEFAULT_PITCHES = {
        "upwork": (
            "Hi {{client_name}},\n\n"
            "I read your post about {{job_title}} — looks like you need {{deliverable}}. "
            "I've shipped similar work ({{portfolio_piece}}), so I have a clear picture "
            "of the pitfalls.\n\n"
            "Quick question so I scope this right: {{clarifying_question}}\n\n"
            "If it helps, I can start with a small milestone this week.\n"
            "— {{your_name}}"
        ),
        "fiverr": (
            "Hi {{client_name}} — I can deliver {{deliverable}} for {{price}} in "
            "{{turnaround}}. Recent similar work: {{portfolio_piece}}. "
            "Send over the details and I'll start today."
        ),
        "freelancer": (
            "Hi {{client_name}},\n\n"
            "Approach for {{job_title}}: {{technical_approach}}.\n\n"
            "Milestones: {{milestone_breakdown}}. Delivery in {{timeline}}.\n"
            "Relevant work: {{portfolio_piece}}.\n"
            "— {{your_name}}"
        ),
        "peopleperhour": (
            "Hi {{client_name}}, {{job_title}} is squarely in my wheelhouse — "
            "here's how I'd tackle it: {{technical_approach}}. "
            "Relevant example: {{portfolio_piece}}. Fixed price {{price}}, "
            "delivered in {{timeline}}."
        ),
        "guru": (
            "Hello {{client_name}},\n\n"
            "On {{job_title}}: {{technical_approach}}. I've done comparable work "
            "({{portfolio_piece}}) and can start {{availability}}.\n\n"
            "Best, {{your_name}}"
        ),
        "linkedin": (
            "Dear {{client_name}},\n\n"
            "I'm applying for {{job_title}}. My background in {{skill_area}} maps "
            "directly to your requirements — specifically {{requirement_1}} and "
            "{{requirement_2}}, which I handled at {{experience}}.\n\n"
            "I'd welcome a short call to discuss fit.\n"
            "Kind regards, {{your_name}}"
        ),
        "indeed": (
            "Dear {{client_name}},\n\n"
            "I'm writing regarding {{job_title}}. I bring {{years}} years of "
            "{{skill_area}} experience, most recently {{experience}}.\n\n"
            "Happy to provide references or a work sample on request.\n"
            "Sincerely, {{your_name}}"
        ),
    }
    for platform, pitch in DEFAULT_PITCHES.items():
        exists = (
            db.query(m.ProfileTemplate)
            .filter(m.ProfileTemplate.user_id == uid,
                    m.ProfileTemplate.platform == platform)
            .count()
        )
        if exists == 0:
            db.add(
                m.ProfileTemplate(
                    user_id=uid,
                    platform=platform,
                    name=f"Default {platform} pitch",
                    pitch_template=pitch,
                )
            )
            created.append(f"profile template ({platform})")

    if db.query(m.PortfolioItem).filter(m.PortfolioItem.user_id == uid).count() == 0:
        db.add(
            m.PortfolioItem(
                user_id=uid,
                title="Sample full-stack project",
                description="Replace with a real project: stack, outcome, and your role.",
                url="",
                tags=["react", "typescript", "node.js"],
            )
        )
        created.append("portfolio item")

    if db.query(m.RateCardEntry).filter(m.RateCardEntry.user_id == uid).count() == 0:
        db.add(
            m.RateCardEntry(
                user_id=uid,
                skill_category="Full-stack web development",
                hourly_rate=75.0,
                fixed_min=1000.0,
                currency="USD",
            )
        )
        created.append("rate card entry")

    if db.query(m.PlatformAccount).filter(m.PlatformAccount.user_id == uid).count() == 0:
        db.add(
            m.PlatformAccount(
                user_id=uid,
                platform="upwork",
                label="Default Upwork (hybrid)",
                principal="agency-manager",
                mode="hybrid",
                enabled=True,
                credential_ref="vault://upwork/agency-manager",
                settings={},
            )
        )
        created.append("platform account")

    db.commit()
    return created


def main() -> None:
    db = SessionLocal()
    try:
        created = seed(db)
        if created:
            print("Seeded defaults:", ", ".join(created))
        else:
            print("All collections already populated — nothing to seed.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
