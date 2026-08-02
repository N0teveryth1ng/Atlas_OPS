# automation script for applying to jobs on Ashby --- playwright will be used over here

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ---- your fixed profile info, edit once here ----
PROFILE = {
    "First Name": "Soham",
    "Last Name": "Das",
    "Email": "dassoham2071@gmail.com",
    "Phone": "+917044855404",
    "LinkedIn": "https://www.linkedin.com/in/sohamdas2071/",
    "GitHub": "https://github.com/N0teveryth1ng",
    "City": "Kolkata",
    "Location": "Kolkata, India",
}

RESUME_PATH = "C:\Users\S Das\Downloads\Soham_Das_Resume [MXT].pdf"


def safe_fill(page, label, value, timeout=2000):
    """Try to fill a field by its visible label. Skip silently if it doesn't exist on this form."""
    try:
        page.get_by_label(label, exact=False).fill(value, timeout=timeout)
        return True
    except PWTimeout:
        return False
    except Exception:
        return False


def safe_upload(page, label, filepath, timeout=2000):
    try:
        page.get_by_label(label, exact=False).set_input_files(filepath, timeout=timeout)
        return True
    except Exception:
        return False


def generate_answer(question_text, resume_text, client):
    """Ask the LLM to answer a custom application question based on the resume."""
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
    """Find any text inputs/textareas not already handled above, and answer them via LLM."""
    # grab all textareas and generic text inputs that still have a label
    labels = page.locator("label").all()
    for label_el in labels:
        try:
            label_text = label_el.inner_text(timeout=1000).strip()
        except Exception:
            continue

        # skip fields we already handled explicitly
        if any(known.lower() in label_text.lower() for known in PROFILE.keys()) or "resume" in label_text.lower():
            continue

        if not label_text:
            continue

        answer = generate_answer(label_text, resume_text, client)
        safe_fill(page, label_text, answer)


def apply_to_ashby(job_url: str, resume_text: str, client) -> dict:
    """
    Applies to a single Ashby job posting.
    Returns a result dict for logging: {url, status, error}
    """
    result = {"url": job_url, "status": "failed", "error": None}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()

        try:
            page.goto(job_url, timeout=15000)

            # click apply if there's a separate apply button (some Ashby pages show the form directly)
            try:
                page.get_by_text("Apply", exact=False).first.click(timeout=3000)
            except Exception:
                pass  # form may already be visible

            # standard fields, by label, skip whatever isn't present
            safe_fill(page, "First Name", PROFILE["First Name"])
            safe_fill(page, "Last Name", PROFILE["Last Name"])
            safe_fill(page, "Email", PROFILE["Email"])
            safe_fill(page, "Phone", PROFILE["Phone"])
            safe_fill(page, "LinkedIn", PROFILE["LinkedIn"])
            safe_fill(page, "GitHub", PROFILE["GitHub"])
            safe_fill(page, "Location", PROFILE["Location"])
            safe_fill(page, "City", PROFILE["City"])

            safe_upload(page, "Resume", RESUME_PATH)

            # anything else on the form (custom questions) gets LLM-generated answers
            fill_custom_questions(page, resume_text, client)

            
            
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

    resume_text = "Backend engineer skilled in Python, FastAPI, PostgreSQL, Redis, Docker..."  # pull from your /upload step

    job_url = "https://jobs.ashbyhq.com/opsmill/f7ef59f0-e2bb-43db-a450-c4e82aebc23d"
    outcome = apply_to_ashby(job_url, resume_text, client)
    print(outcome)