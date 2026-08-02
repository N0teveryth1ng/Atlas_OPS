# SERVER SIDE -- main.py

# loadouts
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from fastapi import UploadFile, File, Form
import json, io
from pypdf import PdfReader

from dotenv import load_dotenv
load_dotenv()





# generative ai 
import os
from groq import Groq

client = Groq(api_key=os.environ["GROQ_API_KEY"])



# intialize server loadouts
app = FastAPI()
templates = Jinja2Templates(directory="templates")



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
        resume_text = "\n".join(page.extract_text() for page in reader.pages)
        
        # build the prompt and call Gemini
        prompt = f"""Extract technical skills, tools, and keywords from this resume as JSON.
        Resume:
        {resume_text}
        
        Return only: {{"keywords": ["skill1", "skill2", ...]}}"""
        
        response = client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[{"role": "user", "content": prompt}]
                )
        raw = response.choices[0].message.content.strip().removeprefix("```json").removesuffix("```").strip()
        data = json.loads(raw)
        
        
        global extracted_keywords
        extracted_keywords = data["keywords"]
        
        
        return templates.TemplateResponse(
            request=request, 
            name="test.html",
            context={
                "filename": resume.filename,
                "keywords": data["keywords"]
            }
        )
        
           
    except Exception as e:
        return HTMLResponse(f"Resume didn't upload ❌ {e}")




# MATCHING ENGINE
from matching_engine import match_resume_to_jd


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









# root test
@app.get("/")
async def root():
    return {"message": "Hello World"}


