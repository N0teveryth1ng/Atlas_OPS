# Simple matching engine: resume keywords vs job description keywords

def match_resume_to_jd(resume_keywords: list[str], jd_text: str, client) -> dict:
    """
    Takes resume keywords (already extracted) and a raw JD string.
    Asks the LLM to extract JD keywords, then computes overlap.
    """

    # extract keywords from the JD 
    jd_prompt = f"""Extract technical skills, tools, and keywords required in this job description as JSON.
    Job Description:
    {jd_text}

    Return only: {{"keywords": ["skill1", "skill2", ...]}}"""

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": jd_prompt}]
    )
    
    
    raw = response.choices[0].message.content.strip().removeprefix("```json").removesuffix("```").strip()

    if not raw:
        raise ValueError("Empty response from Groq for JD extraction")


    import json
    jd_data = json.loads(raw)
    jd_keywords = jd_data["keywords"]

    # normalize both lists 
    resume_set = set(k.lower().strip() for k in resume_keywords)
    jd_set = set(k.lower().strip() for k in jd_keywords)

    # compute the actual match
    matched = resume_set & jd_set          # keywords present in both
    missing = jd_set - resume_set          # JD wants these, resume doesn't have them

    match_score = round(len(matched) / len(jd_set) * 100, 1) if jd_set else 0

    return {
        "match_score": match_score,      
        "matched_keywords": list(matched),
        "missing_keywords": list(missing),
        "should_apply": match_score >= 60   # simple yes/no cutoff
    }


