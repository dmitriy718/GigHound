"""Platform-specific skill taxonomies for auto-suggest.

Curated subsets of each platform's public skill/category lists.
"""
SKILL_TAXONOMY: dict[str, list[str]] = {
    "upwork": [
        "Web Development", "Front-End Development", "Back-End Development",
        "Full-Stack Development", "React", "Next.js", "Node.js", "Python",
        "Django", "FastAPI", "TypeScript", "JavaScript", "WordPress", "Shopify",
        "Mobile App Development", "iOS Development", "Android Development",
        "Machine Learning", "Data Science", "Data Analysis", "AI Chatbot",
        "Prompt Engineering", "DevOps", "AWS", "Docker", "Kubernetes",
        "UI/UX Design", "Figma", "Graphic Design", "Copywriting", "SEO",
        "Content Writing", "Video Editing", "Virtual Assistant",
    ],
    "fiverr": [
        "Logo Design", "Website Development", "WordPress", "Shopify",
        "Social Media Marketing", "SEO", "Voice Over", "Video Editing",
        "Animation", "Illustration", "Copywriting", "Translation",
        "Data Entry", "AI Artists", "ChatGPT Applications", "Webflow",
    ],
    "freelancer": [
        "PHP", "HTML", "CSS", "JavaScript", "Python", "Java", "C#",
        "Mobile App Development", "Graphic Design", "Data Processing",
        "Excel", "Web Scraping", "Software Architecture", "MySQL",
        "Article Writing", "Internet Marketing", "Linux", "AutoCAD",
    ],
    "peopleperhour": [
        "Web Design", "Web Development", "SEO", "Social Media",
        "Content Writing", "Copywriting", "Logo Design", "Branding",
        "Video Production", "Marketing Strategy", "Email Marketing",
    ],
    "guru": [
        "Programming", "Web Development", "Database Development",
        "Design", "Illustration", "Writing", "Editing", "Translation",
        "Engineering", "CAD", "Legal", "Finance", "Admin Support",
    ],
    "linkedin": [
        "Software Engineering", "Product Management", "Data Engineering",
        "Consulting", "Marketing", "Design", "Sales Development",
        "Project Management", "Business Analysis", "Cloud Computing",
    ],
    "indeed": [
        "Software Developer", "Remote Developer", "Contract Developer",
        "Freelance Writer", "Freelance Designer", "Data Analyst",
        "Customer Support", "Administrative Assistant", "QA Engineer",
    ],
}


def suggest_skills(platform: str | None, query: str) -> list[str]:
    q = (query or "").lower()
    pools = (
        {platform: SKILL_TAXONOMY.get(platform, [])}
        if platform in SKILL_TAXONOMY
        else SKILL_TAXONOMY
    )
    out, seen = [], set()
    for skills in pools.values():
        for s in skills:
            if q in s.lower() and s.lower() not in seen:
                seen.add(s.lower())
                out.append(s)
    return sorted(out)[:15]
