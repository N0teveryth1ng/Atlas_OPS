import os
import re

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


PROFILE = {
    "First Name": "Soham",
    "Last Name": "Das",
    "Email": os.environ.get("APPLICANT_EMAIL", ""),
    "Phone": "+917044855404",
    "Location": "Kolkata, India",
    "LinkedIn": "https://www.linkedin.com/in/sohamdas2071/",
    "GitHub": "https://github.com/N0teveryth1ng",
}

RESUME_PATH = r"C:\Users\S Das\Downloads\Soham_Das_Resume [MXT].pdf"


def safe_fill(page, labels, value, timeout=2500):
    for label in labels:
        try:
            page.get_by_label(label, exact=False).fill(value, timeout=timeout)
            return True
        except (PWTimeout, Exception):
            continue
    return False


def safe_upload(page, filepath, timeout=3000):
    for label in ("Resume", "CV", "Resume/CV"):
        try:
            page.get_by_label(label, exact=False).set_input_files(filepath, timeout=timeout)
            return True
        except Exception:
            continue
    try:
        page.locator("input[type='file']").first.set_input_files(filepath, timeout=timeout)
        return True
    except Exception:
        return False


def submit_and_confirm(page):
    try:
        submit_buttons = page.get_by_role("button", name=re.compile(r"submit|send application", re.I))
        if submit_buttons.count():
            submit_buttons.first.click(timeout=5000)
        else:
            page.locator("input[type='submit']").first.click(timeout=5000)
    except Exception:
        return False, False

    confirmation = page.get_by_text(
        re.compile(r"application (has been )?(submitted|received)|thank you for (your )?application|successfully submitted", re.I)
    )
    try:
        return True, confirmation.first.is_visible(timeout=7000)
    except Exception:
        return True, False


def apply_to_generic_ats(job_url: str, resume_text: str, client, email_sender, resume_path: str | None = None) -> dict:
    result = {"url": job_url, "status": "failed", "error": None}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-notifications", "--disable-gpu"])
        page = browser.new_page()
        try:
            page.goto(job_url, timeout=30000, wait_until="domcontentloaded")
            try:
                page.get_by_role("button", name=re.compile(r"apply", re.I)).first.click(timeout=3000)
            except Exception:
                pass

            core_fields = [
                safe_fill(page, ("First Name", "First name"), PROFILE["First Name"]),
                safe_fill(page, ("Last Name", "Last name"), PROFILE["Last Name"]),
                safe_fill(page, ("Email", "Email Address"), PROFILE["Email"]),
            ]
            safe_fill(page, ("Phone", "Phone Number"), PROFILE["Phone"])
            safe_fill(page, ("Location", "City"), PROFILE["Location"])
            safe_fill(page, ("LinkedIn", "LinkedIn Profile"), PROFILE["LinkedIn"])
            safe_fill(page, ("GitHub", "Github"), PROFILE["GitHub"])
            resume_uploaded = safe_upload(page, resume_path or RESUME_PATH)

            if not any(core_fields):
                result["error"] = "Could not identify or fill the application contact fields"
            elif not resume_uploaded:
                result["error"] = "Resume upload did not complete"
            else:
                submitted, confirmed = submit_and_confirm(page)
                if not submitted:
                    result["error"] = "Could not find or click the final submit button"
                elif confirmed:
                    result["status"] = "filled_and_submitted"
                else:
                    result["status"] = "submitted_unconfirmed"
                    result["error"] = "Final submit was clicked, but no confirmation was detected"
        except Exception as exc:
            result["error"] = str(exc)
        finally:
            browser.close()
    return result
