# SERVER SIDE -- main.py

# loadouts
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from fastapi import UploadFile, File, Form
import json, io
from datetime import datetime
from pypdf import PdfReader
import httpx
from resend_mail import email_sender
from playwright.sync_api import sync_playwright
from typing import Optional
import asyncio

from dotenv import load_dotenv
load_dotenv()





# generative ai 
import os
from groq import Groq

client = Groq(api_key=os.environ["GROQ_API_KEY"])



# intialize server loadouts
app = FastAPI()
templates = Jinja2Templates(directory="templates")

extracted_keywords = []
resume_text = ""


# accessing home page
@app.get("/home", response_class=HTMLResponse)
def homing(request: Request):
    return templates.TemplateResponse(
        request=request, 
        name="test.html"
    )
    


# upload ur resume
@app.post("/upload", response_class=HTMLResponse)
async def give_resume(request: Request, resume: UploadFile = File(...)):
    
    try:
        contents = await resume.read()
        
        
        # save inputs
        with open(f"uploads/{resume.filename}", "wb") as f:
            f.write(contents)
        
        
        
        # extract text from the PDF
        reader = PdfReader(io.BytesIO(contents))
        resume_text_content = "\n".join(page.extract_text() for page in reader.pages)
        
        # build the prompt and call Gemini
        prompt = f"""Extract technical skills, tools, and keywords from this resume as JSON.
        Resume:
        {resume_text_content}
        
        Return only: {{"keywords": ["skill1", "skill2", ...]}}"""

        try:
            response = client.chat.completions.create(
                        model="llama-3.3-70b-versatile",
                        messages=[{"role": "user", "content": prompt}]
                    )
            raw = response.choices[0].message.content.strip().removeprefix("```json").removesuffix("```").strip()
            data = json.loads(raw)
            extracted = data.get("keywords", [])
        except Exception as exc:
            print(f"[upload] Groq fallback: {exc}")
            # Fallback: simple keyword extraction from resume text
            resume_text_lower = resume_text_content.lower()
            candidate_keywords = [
                "python", "fastapi", "django", "flask", "postgresql", "mysql", "mongodb",
                "redis", "docker", "kubernetes", "aws", "gcp", "azure", "git", "ci/cd",
                "rest api", "graphql", "microservices", "react", "vue", "angular", "typescript",
                "javascript", "node", "java", "spring", "c#", "dotnet", "ruby", "rails",
            ]
            extracted = [kw for kw in candidate_keywords if kw in resume_text_lower]

        global extracted_keywords, resume_text
        extracted_keywords = extracted
        return templates.TemplateResponse(
            request=request, 
            name="test.html",
            context={
                "filename": resume.filename,
                "keywords": data["keywords"],
                "pipeline_started": True,
            }
        )
        
           
    except Exception as e:
        return HTMLResponse(f"Resume didn't upload ❌ {e}")




# MATCHING ENGINE
from matching_engine import match_resume_to_jd, estimate_title_role_match, detect_experience_levels


def job_text_matches_keywords(job_text: str, resume_keywords: list[str]) -> list[str]:
    resume_set = {kw.lower().strip() for kw in resume_keywords if kw}
    return [kw for kw in resume_set if kw and kw in job_text and kw not in GENERIC_KEYWORDS]


def is_job_relevant(job, resume_keywords):
    if not resume_keywords:
        return True

    text = normalize_job_text(job)
    if not text.strip() or not job.get("job_url"):
        return False

    matched = job_text_matches_keywords(text, resume_keywords)
    if len(matched) < MIN_MATCHED_KEYWORDS:
        return False

    matched_roles = estimate_title_role_match({kw.lower().strip() for kw in resume_keywords if kw}, text)
    resume_levels = detect_experience_levels(" ".join(resume_keywords))
    job_levels = detect_experience_levels(text)

    if "senior" in job_levels and "senior" not in resume_levels and resume_levels:
        return False

    job["match_score"] = round(min(100, len(matched) * 20 + (10 if matched_roles else 0)), 1)
    job["matched_keywords"] = matched
    job["matched_roles"] = matched_roles
    job["matched_responsibilities"] = []
    job["missing_keywords"] = []
    job["should_apply"] = True
    return True


@app.post("/match", response_class=HTMLResponse)
async def match_job(request: Request, jd_text: str = Form(...)):
    result = match_resume_to_jd(
        resume_keywords=extracted_keywords,
        jd_text=jd_text,
        client=client
    )
    return templates.TemplateResponse(
        request=request,
        name="test.html",
        context=result
    )




# find jobs (automation)
SUPPORTED_PATTERNS = ["ashbyhq.com", "lever.co", "jobs.lever.co", "workable.com", "apply.workable.com"]
SUPPORTED_KEYWORDS = ["ashby", "lever", "workable"]


def find_supported_apply_url(html_text: str) -> Optional[str]:
    import re

    candidates = re.findall(r"https?://[^\"'\s]+", html_text)
    for candidate in candidates:
        if any(pattern in candidate for pattern in SUPPORTED_PATTERNS) and "remotive.com/remote-jobs" not in candidate:
            return candidate
    return None


def resolve_remotive_apply_url(remotive_url: str) -> Optional[str]:
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(remotive_url, timeout=30000)
            page.wait_for_load_state("networkidle", timeout=20000)
            html = page.content()
            supported_url = find_supported_apply_url(html)
            browser.close()
            return supported_url
    except Exception:
        return None


def normalize_remotive_job(job):
    return {
        "title": job.get("title"),
        "company": job.get("company_name"),
        "location": job.get("candidate_required_location"),
        "job_url": job.get("url"),
        "description": job.get("description"),
        "source": "remotive",
    }


def normalize_remoteok_job(job):
    return {
        "title": job.get("position"),
        "company": job.get("company"),
        "location": job.get("location"),
        "job_url": job.get("apply_url") or job.get("url"),
        "description": job.get("description"),
        "source": "remoteok",
    }


MIN_MATCHED_KEYWORDS = 2
MAX_PER_SOURCE = 20
MAX_MATCHED_JOBS = 10
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


def normalize_themuse_job(job):
    return {
        "title": job.get("name"),
        "company": job.get("company", {}).get("name"),
        "location": ", ".join(loc.get("name", "") for loc in job.get("locations", [])),
        "job_url": job.get("refs", {}).get("landing_page"),
        "description": job.get("contents"),
        "source": "themuse",
    }


def normalize_job_text(job):
    return " ".join(
        filter(
            None,
            [
                job.get("title", ""),
                job.get("company", ""),
                job.get("location", ""),
                job.get("description", ""),
            ],
        ),
    ).lower()


async def safe_fetch_jobs_json(client, url, headers, timeout=15):
    try:
        resp = await client.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        print(f"[search_jobs] failed to fetch {url}: {exc}")
        return None


@app.get("/find_job")
async def search_jobs():
    headers = {"User-Agent": "Mozilla/5.0 (job search automation)"}
    matched_jobs = []

    try:
        async with httpx.AsyncClient() as client:
            remotive_data = await safe_fetch_jobs_json(client, "https://remotive.com/api/remote-jobs", headers)
            if remotive_data:
                remotive_jobs = remotive_data.get("jobs", [])[:MAX_PER_SOURCE]
                for job in remotive_jobs:
                    normalized = normalize_remotive_job(job)
                    if normalized["job_url"] and is_job_relevant(normalized, extracted_keywords):
                        matched_jobs.append(normalized)
                        if len(matched_jobs) >= MAX_MATCHED_JOBS:
                            break

            if len(matched_jobs) < MAX_MATCHED_JOBS:
                remoteok_data = await safe_fetch_jobs_json(client, "https://remoteok.com/api", headers)
                if isinstance(remoteok_data, list):
                    remoteok_jobs = remoteok_data[:MAX_PER_SOURCE]
                    for job in remoteok_jobs:
                        normalized = normalize_remoteok_job(job)
                        if normalized["job_url"] and is_job_relevant(normalized, extracted_keywords):
                            matched_jobs.append(normalized)
                            if len(matched_jobs) >= MAX_MATCHED_JOBS:
                                break

            if len(matched_jobs) < MAX_MATCHED_JOBS:
                themuse_data = await safe_fetch_jobs_json(client, "https://www.themuse.com/api/public/jobs?page=1", headers)
                if themuse_data:
                    for job in themuse_data.get("results", [])[:MAX_PER_SOURCE]:
                        try:
                            normalized = normalize_themuse_job(job)
                            if normalized["job_url"] and is_job_relevant(normalized, extracted_keywords):
                                matched_jobs.append(normalized)
                                if len(matched_jobs) >= MAX_MATCHED_JOBS:
                                    break
                        except Exception as exc:
                            print(f"[search_jobs] failed to evaluate TheMuse job: {exc}")
    except Exception as exc:
        print(f"[search_jobs] unexpected error: {exc}")
        return []

    return matched_jobs



# aplication scripts
from auto_applications.apply_ahsby import apply_to_ashby
from auto_applications.apply_workable import apply_to_workable


# auto apply to job
def apply_to_job(job_url, resume_text, client, email_sender, resolved=False):
    if "ashbyhq.com" in job_url:
        return apply_to_ashby(job_url, resume_text, client, email_sender)

    elif "workable.com" in job_url:
        return apply_to_workable(job_url, resume_text, client, email_sender)

    elif "remotive.com/remote-jobs" in job_url and not resolved:
        target_url = resolve_remotive_apply_url(job_url)
        if target_url:
            return apply_to_job(target_url, resume_text, client, email_sender, resolved=True)

    return {
        "url": job_url,
        "status": "skipped",
        "error": "No automation available for this job URL"
    }


SUCCESS_TARGET = 3
pipeline_task: Optional[asyncio.Task] = None

pipeline_status = {
    "status": "idle",
    "stage": "waiting",
    "current_job": None,
    "matched_jobs": 0,
    "attempted_applications": 0,
    "filled_not_submitted": 0,
    "skipped": 0,
    "failed": 0,
    "last_error": None,
    "started_at": None,
    "updated_at": None,
}


async def _run_pipeline_worker():
    now = datetime.utcnow().isoformat() + "Z"
    pipeline_status["status"] = "running"
    pipeline_status["stage"] = "searching"
    pipeline_status["current_job"] = None
    pipeline_status["last_error"] = None
    pipeline_status["matched_jobs"] = 0
    pipeline_status["attempted_applications"] = 0
    pipeline_status["filled_not_submitted"] = 0
    pipeline_status["skipped"] = 0
    pipeline_status["failed"] = 0
    pipeline_status["started_at"] = now
    pipeline_status["updated_at"] = now

    try:
        jobs = await search_jobs()
        if isinstance(jobs, dict) and jobs.get("error"):
            raise RuntimeError(jobs["error"])

        pipeline_status["matched_jobs"] = len(jobs)
        pipeline_status["stage"] = "applying"
        pipeline_status["updated_at"] = datetime.utcnow().isoformat() + "Z"
        results = []
        for job in jobs:
            pipeline_status["current_job"] = job.get("job_url")
            pipeline_status["updated_at"] = datetime.utcnow().isoformat() + "Z"
            try:
                outcome = await asyncio.to_thread(apply_to_job, job["job_url"], resume_text, client, email_sender)
            except Exception as e:
                outcome = {"url": job.get("job_url"), "status": "failed", "error": str(e)}
            results.append(outcome)
            pipeline_status["attempted_applications"] = len(results)
            pipeline_status["updated_at"] = datetime.utcnow().isoformat() + "Z"

            if sum(1 for r in results if r.get("status") in ["filled_not_submitted", "filled_and_submitted"]) >= SUCCESS_TARGET:
                break

        stats = {
            "matched_jobs": len(jobs),
            "attempted_applications": len(results),
            "filled_not_submitted": sum(1 for r in results if r.get("status") == "filled_not_submitted"),
            "skipped": sum(1 for r in results if r.get("status") == "skipped"),
            "failed": sum(1 for r in results if r.get("status") == "failed"),
        }

        pipeline_status.update(stats)
        pipeline_status["stage"] = "finished"
        pipeline_status["current_job"] = None
        pipeline_status["status"] = "completed"
        pipeline_status["updated_at"] = datetime.utcnow().isoformat() + "Z"

        try:
            email_sender(results)
        except Exception as email_err:
            pipeline_status["status"] = "completed_with_email_error"
            pipeline_status["last_error"] = str(email_err)

        return {
            "summary": stats,
            "jobs": results
        }

    except Exception as exc:
        pipeline_status["status"] = "failed"
        pipeline_status["stage"] = "failed"
        pipeline_status["current_job"] = None
        pipeline_status["last_error"] = str(exc)
        pipeline_status["updated_at"] = datetime.utcnow().isoformat() + "Z"
        return {"error": str(exc)}


@app.get("/run_pipeline")
async def run_pipeline():
    global pipeline_task
    if pipeline_status["status"] == "running":
        return {
            "status": "already_running",
            "pipeline_status": pipeline_status,
        }

    pipeline_task = asyncio.create_task(_run_pipeline_worker())
    pipeline_task.add_done_callback(lambda task: nonlocal_pipeline_task())
    return {"status": "started", "message": "Pipeline is running in the background."}


def nonlocal_pipeline_task():
    global pipeline_task
    pipeline_task = None


@app.get("/cancel_pipeline")
async def cancel_pipeline():
    global pipeline_task
    if pipeline_task is None or pipeline_task.done():
        return {"status": "no_pipeline", "message": "No active pipeline to cancel."}

    pipeline_task.cancel()
    pipeline_status["status"] = "cancelled"
    pipeline_status["stage"] = "cancelled"
    pipeline_status["current_job"] = None
    pipeline_status["updated_at"] = datetime.utcnow().isoformat() + "Z"
    return {"status": "cancelled", "message": "Pipeline cancellation requested."}


@app.get("/pipeline_status")
async def pipeline_status_endpoint():
    return pipeline_status


# root test
@app.get("/")
async def root():
    return {"message": "Hello World"}


