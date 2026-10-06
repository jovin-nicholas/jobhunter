"""A small skill table for the matched-skills / keyword-gaps lists shown in notifications."""
from __future__ import annotations

from jobhunter.text_match import has_term, word_text

# Canonical skill -> aliases. Order is the order shown to the user.
SKILLS = {
    "python": ["python", "python3"],
    "javascript": ["javascript", "js"],
    "typescript": ["typescript", "ts"],
    "react": ["react", "reactjs"],
    "nextjs": ["next.js", "nextjs"],
    "nodejs": ["node.js", "nodejs", "node"],
    "java": ["java"],
    "spring boot": ["spring boot", "springboot", "spring"],
    "sql": ["sql"],
    "postgresql": ["postgresql", "postgres"],
    "redis": ["redis"],
    "aws": ["aws", "amazon web services"],
    "docker": ["docker"],
    "kubernetes": ["kubernetes", "k8s"],
    "fastapi": ["fastapi"],
    "flask": ["flask"],
    "rest api": ["rest api", "restapis", "api design"],
    "machine learning": ["machine learning", "ml"],
    "ai": ["ai", "artificial intelligence"],
    "pytorch": ["pytorch", "torch"],
    "tensorflow": ["tensorflow"],
    "llm": ["llm", "large language model", "language model"],
    "rag": ["rag", "retrieval augmented generation"],
    "data engineering": ["data engineering", "data pipeline"],
    "backend": ["backend"],
    "frontend": ["frontend"],
    "fullstack": ["fullstack", "full-stack"],
    "microservices": ["microservices"],
}


def skills_in(text: str) -> list[str]:
    words = word_text(text)
    return [skill for skill, aliases in SKILLS.items() if any(has_term(words, a) for a in aliases)]


def compare_skills(job_text: str, resume_text: str) -> tuple[list[str], list[str]]:
    """(skills the job asks for that the resume has, skills the job asks for that the resume lacks)."""
    have = set(skills_in(resume_text))
    wanted = skills_in(job_text)
    return [s for s in wanted if s in have], [s for s in wanted if s not in have]
