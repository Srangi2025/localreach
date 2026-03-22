"""
LocalReach — FREE Webhook Server (Deploy to Render.com free tier)
Receives Bland.ai call outcomes, updates CRM JSON, logs call notes.
SMS sending uses Bland's built-in SMS (free) instead of Twilio.

Deploy:
  1. Push to GitHub
  2. render.com → New Web Service → connect repo
  3. Build command: pip install -r requirements.txt
  4. Start command: uvicorn webhook_free:app --host 0.0.0.0 --port $PORT
  5. Add env vars in Render dashboard

Local dev:
  uvicorn webhook_free:app --reload --port 8000
  # Then in another terminal: npx localtunnel --port 8000
  # Copy the URL → set as WEBHOOK_URL in .env
"""

import os
import json
import logging
import re
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────
CALENDLY_LINK = os.getenv("CALENDLY_LINK", "https://calendly.com/yourname/demo")
YOUR_NAME     = os.getenv("YOUR_NAME", "Alex")
YOUR_COMPANY  = os.getenv("YOUR_COMPANY", "LocalReach Web Studio")
BLAND_API_KEY = os.getenv("BLAND_API_KEY", "")
CRM_JSON_PATH = os.getenv("CRM_JSON_PATH", "crm_leads.json")

# Render gives you a persistent disk — but on free tier the filesystem
# resets on redeploy. For persistence across deploys, set CRM_JSON_PATH
# to a mounted disk path, OR use the /leads POST endpoint to sync from
# your local machine after each scraper run.

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("localreach")

app = FastAPI(title="LocalReach Webhook — Free Tier")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── CRM helpers ────────────────────────────────────────────────────────────────

def load_leads() -> list:
    if not os.path.exists(CRM_JSON_PATH):
        return []
    with open(CRM_JSON_PATH) as f:
        try:
            return json.load(f)
        except Exception:
            return []

def save_leads(leads: list):
    with open(CRM_JSON_PATH, "w") as f:
        json.dump(leads, f, indent=2)

def find_lead(leads: list, lead_id: str = "", biz_name: str = "") -> Optional[dict]:
    if lead_id:
        for l in leads:
            if str(l.get("id","")) == str(lead_id):
                return l
    if biz_name:
        bn = biz_name.lower()
        for l in leads:
            if l.get("name","").lower() == bn:
                return l
    return None

def add_note(lead: dict, text: str):
    months = ["Jan","Feb","Mar","Apr","May","Jun",
              "Jul","Aug","Sep","Oct","Nov","Dec"]
    d = datetime.now()
    if "notes" not in lead:
        lead["notes"] = []
    lead["notes"].insert(0, {
        "date": f"{months[d.month-1]} {d.day}",
        "text": text
    })

# ── Status map ─────────────────────────────────────────────────────────────────
STATUS_MAP = {
    "demo_booked":        "demo",
    "interested":         "interested",
    "callback_requested": "called",
    "not_interested":     "noint",
    "no_answer":          "noans",
    "voicemail":          "noans",
    "wrong_number":       "noans",
}

# ── SMS via Bland (free, no Twilio needed) ─────────────────────────────────────

def send_bland_sms(to_number: str, message: str) -> bool:
    """
    Bland.ai has a free SMS endpoint — uses your Bland account's pool number.
    No separate Twilio account needed.
    """
    if not BLAND_API_KEY:
        log.warning("No Bland API key — SMS not sent")
        return False
    try:
        import requests as req
        resp = req.post(
            "https://api.bland.ai/v1/sms/send",
            headers={"authorization": BLAND_API_KEY},
            json={"to": to_number, "message": message},
            timeout=8
        )
        if resp.ok:
            log.info(f"SMS sent to {to_number}")
            return True
        else:
            log.warning(f"Bland SMS failed: {resp.text[:200]}")
            return False
    except Exception as e:
        log.error(f"SMS error: {e}")
        return False

# ── Transcript parser ──────────────────────────────────────────────────────────

def summarise(transcript) -> str:
    if not transcript:
        return "No transcript available."
    if isinstance(transcript, str):
        return transcript[:600]
    if isinstance(transcript, list):
        lines = []
        for turn in transcript:
            role = str(turn.get("role","?")).capitalize()
            msg  = str(turn.get("text", turn.get("content", turn.get("message","")))).strip()
            if msg:
                lines.append(f"{role}: {msg}")
        text = " | ".join(lines)
        return text[:700] if len(text) > 700 else text
    return str(transcript)[:400]

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    leads = load_leads()
    demos = sum(1 for l in leads if l.get("status") == "demo")
    return {"status": "ok", "leads": len(leads), "demos": demos}


@app.get("/leads")
async def get_leads():
    """The LocalReach dashboard can poll this to get live data."""
    return JSONResponse(content=load_leads())


@app.post("/leads")
async def push_leads(request: Request):
    """
    Push leads from your local machine to the server after scraping.
    Usage: curl -X POST https://your-app.onrender.com/leads \
             -H 'Content-Type: application/json' \
             -d @crm_leads.json
    """
    try:
        new_leads = await request.json()
        if not isinstance(new_leads, list):
            return JSONResponse({"error": "Expected a JSON array"}, status_code=400)

        existing = load_leads()
        existing_ids = {str(l.get("id","")) for l in existing}
        added = [l for l in new_leads if str(l.get("id","")) not in existing_ids]
        merged = added + existing
        save_leads(merged)
        log.info(f"Pushed {len(added)} new leads ({len(existing)} existing)")
        return JSONResponse({"added": len(added), "total": len(merged)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/webhook/bland")
async def bland_webhook(request: Request):
    """
    Main Bland.ai webhook.
    Bland sends a POST after every call with full transcript + analysis.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)

    # Bland webhook structure
    call_id    = body.get("call_id", "")
    status     = body.get("status", "")           # "completed", "no-answer", etc.
    transcript = body.get("transcripts", body.get("transcript", []))
    analysis   = body.get("analysis", {}) or {}
    metadata   = body.get("metadata",  {}) or {}
    duration   = body.get("call_length", 0) or 0

    lead_id    = str(metadata.get("lead_id", ""))
    biz_name   = metadata.get("biz_name", "")

    # Extract structured analysis (from analysis_schema we sent)
    outcome      = analysis.get("outcome", "")
    owner_name   = analysis.get("owner_name", "")
    callback_time= analysis.get("callback_time", "")
    key_notes    = analysis.get("key_notes", "")

    log.info(f"Bland webhook: call_id={call_id} status={status} outcome={outcome} lead={biz_name}")

    # Infer outcome from status if analysis didn't catch it
    if not outcome:
        if status in ("no-answer", "failed"):
            outcome = "no_answer"
        elif status == "voicemail":
            outcome = "voicemail"
        else:
            outcome = "called"

    # Build note
    summary   = summarise(transcript)
    note_parts = [f"[BLAND] {status} ({int(duration)}s) — {outcome}"]
    if owner_name:
        note_parts.append(f"Owner: {owner_name}")
    if key_notes:
        note_parts.append(key_notes)
    if callback_time:
        note_parts.append(f"Callback: {callback_time}")
    note_parts.append(summary)
    full_note = " | ".join(note_parts)

    # Update CRM
    leads = load_leads()
    lead  = find_lead(leads, lead_id=lead_id, biz_name=biz_name)

    if lead:
        new_status = STATUS_MAP.get(outcome, "called")
        lead["status"] = new_status
        lead["bland_call_id"] = call_id
        add_note(lead, full_note)

        # Send Calendly SMS if demo was booked
        if outcome == "demo_booked":
            phone = lead.get("raw_phone") or lead.get("phone", "")
            if phone:
                name_part = owner_name or lead["name"]
                sms_body = (
                    f"Hi {name_part}! This is {YOUR_NAME} from {YOUR_COMPANY} — "
                    f"great chatting with you! "
                    f"Here's the link to pick a time that works: {CALENDLY_LINK} "
                    f"Looking forward to showing you what I have in mind!"
                )
                send_bland_sms(phone, sms_body)
                add_note(lead, f"[AUTO] Calendly SMS sent to {phone}")

        save_leads(leads)
        log.info(f"Updated '{lead.get('name')}' → {new_status}")
    else:
        # Lead not found — store as orphan note so nothing is lost
        log.warning(f"Lead not found: id={lead_id} name={biz_name}. Storing orphan.")
        orphan = {
            "id":       int(call_id[:8], 16) if call_id else 0,
            "name":     biz_name or f"Unknown ({call_id[:8]})",
            "type":     metadata.get("biz_type", "Business"),
            "phone":    "",
            "area":     "",
            "score":    50,
            "status":   STATUS_MAP.get(outcome, "called"),
            "missing":  [],
            "notes":    [{"date": datetime.now().strftime("%b %-d"), "text": full_note}],
            "mapsUrl":  "",
            "bland_call_id": call_id,
        }
        leads.insert(0, orphan)
        save_leads(leads)

    return JSONResponse({"received": True, "outcome": outcome})


@app.post("/webhook/bland/sms")
async def manual_sms(request: Request):
    """
    Manually trigger a Calendly SMS to a lead.
    Usage: POST /webhook/bland/sms  { "lead_id": "123", "phone": "+15550001234" }
    """
    body     = await request.json()
    lead_id  = str(body.get("lead_id", ""))
    phone    = body.get("phone", "")
    leads    = load_leads()
    lead     = find_lead(leads, lead_id=lead_id)

    if not lead and not phone:
        return JSONResponse({"error": "lead_id or phone required"}, status_code=400)

    phone = phone or lead.get("raw_phone") or lead.get("phone","")
    name  = lead.get("name","there") if lead else "there"

    msg = (
        f"Hi! This is {YOUR_NAME} from {YOUR_COMPANY}. "
        f"Here's the link to book your free demo call: {CALENDLY_LINK} — talk soon!"
    )
    success = send_bland_sms(phone, msg)
    return JSONResponse({"sent": success, "to": phone})


# ── Startup ────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    leads = load_leads()
    log.info(f"LocalReach webhook started. {len(leads)} leads in CRM.")
    log.info(f"Calendly link: {CALENDLY_LINK}")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("webhook_free:app", host="0.0.0.0", port=port, reload=False)
