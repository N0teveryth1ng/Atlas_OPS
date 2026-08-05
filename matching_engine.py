# Simple matching engine: resume keywords vs job description keywords and responsibilities

GENERIC_KEYWORDS = {
    "remote",
    "job",
    "jobs",
    "work",
    "working",
    "experience",
    "skills",
    "team",
    "communication",
    "full-time",
    "part-time",
    "company",
    "project",
    "projects",
    "developer",
    "engineer",
    "software",
    "technology",
    "technical",
}

ROLE_TERMS = {
    "backend",
    "frontend",
    "fullstack",
    "devops",
    "data",
    "machine learning",
    "ml",
    "cloud",
    "security",
    "sre",
    "qa",
    "mobile",
    "site reliability",
    "product",
}

SENIOR_LEVEL_TERMS = {"senior", "sr", "lead", "principal", "manager", "director", "vp", "architect"}
JUNIOR_LEVEL_TERMS = {"junior", "jr", "associate", "entry", "intern"}


def detect_experience_levels(text: str) -> set[str]:
    text = text.lower()
    levels = set()
    if any(term in text for term in SENIOR_LEVEL_TERMS):
        levels.add("senior")
    if any(term in text for term in JUNIOR_LEVEL_TERMS):
        levels.add("junior")
    return levels


def estimate_title_role_match(resume_set: set[str], jd_text: str) -> list[str]:
    jd_text = jd_text.lower()
    return [term for term in ROLE_TERMS if term in resume_set and term in jd_text]


def match_resume_to_jd(resume_keywords: list[str], jd_text: str, client) -> dict:
    """
    Takes resume keywords (already extracted) and a raw JD string.
    Asks the LLM to extract JD keywords and responsibilities, then computes overlap.
    """

    jd_prompt = f"""Extract technical skills, tools, keywords, and responsibilities from this job description as JSON.
    Job Description:
    {jd_text}

    Return only: {{
      "keywords": ["skill1", "skill2", ...],
      "responsibilities": ["responsibility phrase 1", "responsibility phrase 2"]
    }}"""

    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": jd_prompt}]
        )

        raw = response.choices[0].message.content.strip().removeprefix("```json").removesuffix("```").strip()

        if not raw:
            raise ValueError("Empty JD extraction response")

        import json
        jd_data = json.loads(raw)
        jd_keywords = jd_data.get("keywords", []) or []
        jd_responsibilities = jd_data.get("responsibilities", []) or []
    except Exception as exc:
        # Fallback to a local text-based relevance heuristic when the LLM is unavailable.
        jd_keywords = []
        jd_responsibilities = []
        jd_text_lower = jd_text.lower()
        resume_set = {k.lower().strip() for k in resume_keywords if k}
        jd_keywords = [kw for kw in resume_set if kw and kw in jd_text_lower and kw not in GENERIC_KEYWORDS]
        jd_responsibilities = [kw for kw in resume_set if kw and kw in jd_text_lower and kw not in GENERIC_KEYWORDS]
        if hasattr(exc, 'args') and exc.args:
            print(f"[matching_engine] Groq fallback: {exc}")

    resume_set = {k.lower().strip() for k in resume_keywords if k}
    jd_set = {k.lower().strip() for k in jd_keywords if k}
    responsibility_text = " ".join(r.lower().strip() for r in jd_responsibilities)

    matched_keywords = [kw for kw in resume_set if kw not in GENERIC_KEYWORDS and kw in jd_set]
    matched_responsibilities = [kw for kw in resume_set if kw not in GENERIC_KEYWORDS and kw in responsibility_text]
    matched_roles = estimate_title_role_match(resume_set, jd_text)
    missing_keywords = list(jd_set - resume_set)

    keyword_score = len(matched_keywords) / len(jd_set) if jd_set else 0
    responsibility_score = len(matched_responsibilities) / len(resume_set) if resume_set else 0
    role_score = min(1.0, len(matched_roles) / 2)

    resume_levels = detect_experience_levels(" ".join(resume_keywords))
    job_levels = detect_experience_levels(jd_text)
    level_penalty = 0.0
    if "senior" in job_levels and "senior" not in resume_levels and resume_levels:
        level_penalty = 0.1

    match_score = round(max(0, keyword_score * 0.55 + responsibility_score * 0.25 + role_score * 0.2 - level_penalty) * 100, 1)

    return {
        "match_score": match_score,
        "matched_keywords": matched_keywords,
        "matched_responsibilities": matched_responsibilities,
        "matched_roles": matched_roles,
        "missing_keywords": missing_keywords,
        "responsibilities": jd_responsibilities,
        "job_levels": list(job_levels),
        "resume_levels": list(resume_levels),
        "should_apply": match_score >= 60 and (len(matched_keywords) >= 2 or len(matched_responsibilities) >= 1 or len(matched_roles) >= 1)
    }


