# automation script for applying to jobs on Workable --- playwright will be used over here

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

PROFILE = {
    "First name": "Soham",
    "Last name": "Das",
    "Email": "dassoham2071@gmail.com",
    "Phone": "+91 7044855404",
    "Location": "Kolkata, India",
    "LinkedIn": "https://www.linkedin.com/in/sohamdas2071/",
    "GitHub": "https://github.com/N0teveryth1ng",
}

RESUME_PATH = "C:\Users\S Das\Downloads\Soham_Das_Resume [MXT].pdf"


def safe_fill(page, label, value, timeout=2000):
    try:
        page.get_by_label(label, exact=False).fill(value, timeout=timeout)
        return True
    except (PWTimeout, Exception):
        return False


def safe_upload(page, label, filepath, timeout=2000):
    try:
        page.get_by_label(label, exact=False).set_input_files(filepath, timeout=timeout)
        return True
    except Exception:
        return False


def generate_answer(question_text, resume_text, client):
    prompt = f"""You are filling out a job application. Answer this question briefly (2-4 sentences),
    in first person, based on this resume. Be specific and honest, not generic.

    Question: {question_text}

    Resume:
    {resume_text}

    Return only the answer text, nothing else."""

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}]
    )
    return response.choices[0].message.content.strip()


def fill_custom_questions(page, resume_text, client):
    labels = page.locator("label").all()
    for label_el in labels:
        try:
            label_text = label_el.inner_text(timeout=1000).strip()
        except Exception:
            continue

        if any(known.lower() in label_text.lower() for known in PROFILE.keys()) or "resume" in label_text.lower() or "cv" in label_text.lower():
            continue

        if not label_text:
            continue

        answer = generate_answer(label_text, resume_text, client)
        safe_fill(page, label_text, answer)


def apply_to_workable(job_url: str, resume_text: str, client) -> dict:
    """
    Applies to a single Workable job posting (apply.workable.com/...).
    Returns a result dict for logging: {url, status, error}
    """
    result = {"url": job_url, "status": "failed", "error": None}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()

        try:
            page.goto(job_url, timeout=15000)

            try:
                page.get_by_role("link", name="Apply", exact=False).first.click(timeout=3000)
            except Exception:
                pass

            safe_fill(page, "First name", PROFILE["First name"])
            safe_fill(page, "Last name", PROFILE["Last name"])
            safe_fill(page, "Email", PROFILE["Email"])
            safe_fill(page, "Phone", PROFILE["Phone"])
            safe_fill(page, "Location", PROFILE["Location"])
            safe_fill(page, "LinkedIn", PROFILE["LinkedIn"])
            safe_fill(page, "GitHub", PROFILE["GitHub"])

            safe_upload(page, "Resume", RESUME_PATH)

            fill_custom_questions(page, resume_text, client)

            # NOTE: submit is commented out on purpose during testing.
            # Uncomment only once you've verified the form fills correctly.
            # page.click("button[type='submit']")

            result["status"] = "filled_not_submitted"

        except Exception as e:
            result["error"] = str(e)

        finally:
            browser.close()

    return result


if __name__ == "__main__":
    from groq import Groq
    import os
    from dotenv import load_dotenv
    load_dotenv()

    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    resume_text = "Backend engineer skilled in Python, FastAPI, PostgreSQL, Redis, Docker..."

    job_url = "https://apply.workable.com/example-company/j/ABCDEF1234/"
    outcome = apply_to_workable(job_url, resume_text, client)
    print(outcome)