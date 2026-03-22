"""
LocalReach — FREE AI Voice Agent (Bland.ai)
Bland.ai free tier: 100 minutes/month, no credit card required.
Sign up at: https://app.bland.ai

Setup:
  pip install requests python-dotenv

Usage:
  python agent_free.py                    # Call all 'new' leads
  python agent_free.py --limit 5          # Call first 5
  python agent_free.py --dry-run          # Preview without dialing
"""

import os
import json
import time
import argparse
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
BLAND_API_KEY    = os.getenv("BLAND_API_KEY", "YOUR_BLAND_KEY")
WEBHOOK_URL      = os.getenv("WEBHOOK_URL", "https://your-app.onrender.com/webhook/bland")
CALENDLY_LINK    = os.getenv("CALENDLY_LINK", "https://calendly.com/yourname/demo")
YOUR_NAME        = os.getenv("YOUR_NAME", "Alex")
YOUR_COMPANY     = os.getenv("YOUR_COMPANY", "LocalReach Web Studio")
YOUR_PHONE       = os.getenv("YOUR_PHONE", "")   # Your real number for callback SMS
CRM_JSON_PATH    = "crm_leads.json"
CALL_GAP_SECONDS = 90    # Gap between calls

BLAND_BASE = "https://api.bland.ai/v1"
HEADERS    = {"authorization": BLAND_API_KEY, "Content-Type": "application/json"}

# ── Pathway (Bland's conversation flow format) ────────────────────────────────
# Bland uses a "pathway" structure: a graph of nodes connected by conditions.
# This is more reliable than a single prompt because each stage is explicit.
# Each node has: id, name, prompt, transitions (conditions → next node id)

def build_pathway(biz_name: str, biz_type: str, missing: list) -> dict:
    missing_desc = " and ".join({
        "website": "no website",
        "gmb":     "an incomplete Google Business Profile",
        "reviews": "very few reviews",
        "social":  "no social media presence",
        "seo":     "not showing up on local Google searches"
    }.get(m, m) for m in (missing or ["website"]))

    value_lines = {
        "Restaurant":    "87% of people search Google before deciding where to eat. Right now your competitors are showing up and you're not.",
        "Retail":        "People in your area are actively searching for what you sell. Without a website, those customers go to whoever shows up first.",
        "Salon":         "Salons with online booking see 40% fewer no-shows and pick up new clients they'd never have reached otherwise.",
        "Auto Shop":     "When someone's car breaks down they Google the nearest shop. Without a website you're invisible to every one of those searches.",
        "Plumber":       "People search Google when they have an urgent need. If you're not showing up, those emergency calls go straight to your competitors.",
        "Gym":           "When someone decides to get fit they Google local gyms. A website with your class schedule and pricing is often what makes them pick up the phone.",
        "Medical":       "New patients almost always search online before choosing a provider. Without a website you're not even in the consideration set.",
    }
    value = value_lines.get(biz_type, "Most of your potential customers search Google before deciding who to call. Without a website you're invisible to them.")

    return {
        "name": f"LocalReach Cold Call — {biz_name}",
        "nodes": [
            {
                "id": "opening",
                "name": "Opening",
                "type": "conversation",
                "prompt": (
                    f"You are {YOUR_NAME}, a friendly local web developer calling from {YOUR_COMPANY}. "
                    f"You are calling {biz_name}, a local {biz_type}. "
                    f"Start with: 'Hi, is this the owner or manager of {biz_name}? "
                    f"Great! My name is {YOUR_NAME} — I'm a local web developer. Do you have about 60 seconds?' "
                    f"If they say they're busy, ask: 'No problem at all — what's a better time to reach you?' and note the time. "
                    f"If they say yes or seem open, transition to the hook."
                ),
                "transitions": [
                    {"condition": "busy or bad time",   "next": "schedule_callback"},
                    {"condition": "yes or open to talk", "next": "hook"},
                    {"condition": "wrong number or not the owner", "next": "wrap_up_neutral"},
                ]
            },
            {
                "id": "hook",
                "name": "The Hook",
                "type": "conversation",
                "prompt": (
                    f"You noticed that {biz_name} has {missing_desc}. "
                    f"Say: 'I was looking up {biz_type}s in the area and noticed {biz_name} doesn't have [their specific gap]. "
                    f"I wanted to reach out personally because I think there's a real opportunity here for you.' "
                    f"Then immediately deliver the value line: '{value}' "
                    f"Keep it natural and conversational — one sentence at a time."
                ),
                "transitions": [
                    {"condition": "interested or asks a question", "next": "offer"},
                    {"condition": "not interested",                "next": "objection_handler"},
                    {"condition": "already has a website or solution", "next": "objection_has_solution"},
                ]
            },
            {
                "id": "offer",
                "name": "The Offer",
                "type": "conversation",
                "prompt": (
                    f"Present the offer warmly. Say: "
                    f"'I build professional websites for local businesses starting at just $499 — "
                    f"that's a one-time fee, no monthly charges to start. "
                    f"I also set up your Google listing so you actually show up when people search nearby. "
                    f"One new customer from Google pays for the whole thing.' "
                    f"Pause and let them respond before moving to the close."
                ),
                "transitions": [
                    {"condition": "sounds good, interested, tell me more", "next": "book_demo"},
                    {"condition": "too expensive, can't afford it",         "next": "objection_price"},
                    {"condition": "not interested, don't need it",          "next": "objection_handler"},
                    {"condition": "already have someone, have a plan",      "next": "objection_has_solution"},
                ]
            },
            {
                "id": "book_demo",
                "name": "Book Demo",
                "type": "conversation",
                "prompt": (
                    f"Close warmly for the demo. Say: "
                    f"'I'd love to show you exactly what I'd build for {biz_name} — "
                    f"it's a free 20-minute call, no pressure and no commitment. "
                    f"I'll put together a quick mockup of your site before we talk so you can see exactly what you'd be getting. "
                    f"Would sometime this week or next work for you?' "
                    f"When they agree, say: "
                    f"'Perfect! I'll send you a quick text right now with a link to pick a time that works — "
                    f"what's the best number for that text?' "
                    f"Confirm their number and proceed to send the SMS."
                ),
                "transitions": [
                    {"condition": "gives phone number or confirms",  "next": "send_sms_confirm"},
                    {"condition": "declines or not ready to commit", "next": "soft_close"},
                ]
            },
            {
                "id": "send_sms_confirm",
                "name": "SMS Confirmed",
                "type": "conversation",
                "prompt": (
                    f"Confirm the booking warmly: "
                    f"'Great! I'm sending that link right now. "
                    f"I'll prepare a quick mockup of what {biz_name}'s site could look like before we talk — "
                    f"you're going to love it. Have a great rest of your day!' "
                    f"Then end the call positively."
                ),
                "transitions": [
                    {"condition": "any", "next": "end_demo_booked"}
                ]
            },
            {
                "id": "soft_close",
                "name": "Soft Close",
                "type": "conversation",
                "prompt": (
                    f"They're interested but not ready to commit to a specific time. "
                    f"Say: 'Totally understand — would it be okay if I sent you a quick text with "
                    f"a couple examples of sites I've done for similar businesses? "
                    f"You can take a look whenever you have a moment, no rush at all.' "
                    f"If they agree, confirm their number for the text."
                ),
                "transitions": [
                    {"condition": "agrees to text",  "next": "send_sms_confirm"},
                    {"condition": "declines",        "next": "wrap_up_neutral"},
                ]
            },
            {
                "id": "objection_handler",
                "name": "General Objection",
                "type": "conversation",
                "prompt": (
                    "Handle the objection naturally using one of these responses:\n\n"
                    "If 'I get enough business from word of mouth':\n"
                    "'That's a great sign — it means people love what you do. "
                    "Here's the thing: when someone gets a referral for your business, the first thing they do is Google you. "
                    "If nothing comes up, some of those referrals just go cold. A website makes sure your reputation shows up when people look.'\n\n"
                    "If 'I don't have time for this':\n"
                    "'I completely get it — I'll keep this super short. The whole setup takes me about a week and you don't have to do anything. "
                    "Would it be worth a 20-minute call just to see what it would look like?'\n\n"
                    "If general resistance:\n"
                    "'Totally fair — I'm not here to pressure anyone. Would it be okay if I sent you a quick text with "
                    "a couple examples of what I've done for similar businesses nearby? Just to have on hand.'\n\n"
                    "After handling, try to re-engage toward the demo or a soft close."
                ),
                "transitions": [
                    {"condition": "opens up or shows interest",  "next": "book_demo"},
                    {"condition": "still not interested",        "next": "wrap_up_neutral"},
                    {"condition": "agrees to text/info",         "next": "soft_close"},
                ]
            },
            {
                "id": "objection_price",
                "name": "Price Objection",
                "type": "conversation",
                "prompt": (
                    "Handle the price objection: "
                    "'I completely understand — that's actually why I started doing this differently. "
                    "Most agencies charge three to five thousand dollars. "
                    "I charge $499 because I want to build long-term relationships with local businesses, not one-time paydays. "
                    "And honestly, if you get just one extra customer a month from Google, "
                    "the site has paid for itself — and that's a very low bar for most businesses.' "
                    "Then say: 'Would it be worth a quick 20-minute call just to see what it would look like for you?'"
                ),
                "transitions": [
                    {"condition": "interested or open",       "next": "book_demo"},
                    {"condition": "still too expensive",      "next": "wrap_up_neutral"},
                ]
            },
            {
                "id": "objection_has_solution",
                "name": "Has Website/Solution",
                "type": "conversation",
                "prompt": (
                    "They already have something in place. "
                    "Say: 'Oh great — I didn't see it come up in my search so I wanted to check. "
                    "Is it showing up well when people search for [their type] in [their area]? "
                    "Sometimes sites exist but aren't optimized for local search — "
                    "that's actually something I specialize in fixing.' "
                    "If they say it's fine, be gracious: "
                    "'That's great to hear! If anything changes or you ever want a second set of eyes on it, "
                    "my name is [YOUR_NAME] from [YOUR_COMPANY]. Good luck with everything!'"
                ),
                "transitions": [
                    {"condition": "open to SEO or improvements", "next": "offer"},
                    {"condition": "happy with current setup",    "next": "wrap_up_neutral"},
                ]
            },
            {
                "id": "schedule_callback",
                "name": "Schedule Callback",
                "type": "conversation",
                "prompt": (
                    "They're busy. Get a callback time. "
                    "Say: 'No problem at all — I appreciate your time. "
                    "What would be a better time for me to reach you? Morning or afternoon usually better?' "
                    "Note the time they give. "
                    "Say: 'Perfect — I'll call back [their time]. Have a great day!'"
                ),
                "transitions": [
                    {"condition": "gives a time", "next": "end_callback"},
                    {"condition": "any",          "next": "wrap_up_neutral"},
                ]
            },
            {
                "id": "wrap_up_neutral",
                "name": "Wrap Up",
                "type": "conversation",
                "prompt": (
                    f"End the call gracefully. "
                    f"Say: 'Totally understand — I appreciate you taking the time. "
                    f"My name is {YOUR_NAME} from {YOUR_COMPANY} if anything ever changes. "
                    f"Good luck with {biz_name}!' "
                    f"Then end the call."
                ),
                "transitions": [
                    {"condition": "any", "next": "end_not_interested"}
                ]
            },
            {
                "id": "end_demo_booked",
                "name": "END — Demo Booked",
                "type": "end",
                "prompt": "Call complete. Outcome: demo_booked. Send SMS with Calendly link."
            },
            {
                "id": "end_callback",
                "name": "END — Callback Scheduled",
                "type": "end",
                "prompt": "Call complete. Outcome: callback_requested."
            },
            {
                "id": "end_not_interested",
                "name": "END — Not Interested",
                "type": "end",
                "prompt": "Call complete. Outcome: not_interested."
            }
        ],
        "start_node_id": "opening",
        "global_prompt": (
            f"You are {YOUR_NAME}, a local web developer from {YOUR_COMPANY}. "
            f"You are warm, genuine, and conversational — you sound like a real person, not a call center. "
            f"You speak naturally with normal pauses. You never read from a script mechanically. "
            f"You are calling {biz_name}, a {biz_type}. "
            f"Your goal is to book a free 20-minute demo call. "
            f"Max call length: 4 minutes. "
            f"If directly asked 'Are you a robot or AI?', say: "
            f"'I use AI tools to help me work efficiently, but I'm the one making this call.' "
            f"Always be respectful of their time. Never be pushy."
        )
    }

# ── Voicemail script ───────────────────────────────────────────────────────────

def voicemail_message(biz_name: str, biz_type: str) -> str:
    return (
        f"Hi, this message is for the owner of {biz_name}. "
        f"My name is {YOUR_NAME} — I'm a local web developer in the area. "
        f"I noticed {biz_name} doesn't have a website yet and I had a couple of ideas "
        f"specifically for {biz_type}s that could really help you get more calls from Google. "
        f"I've helped similar local businesses in the area and I'd love to show you what I did for them — "
        f"takes about 15 minutes. "
        f"Give me a call or text back whenever works for you. Thanks!"
    )

# ── Dispatch a call via Bland ─────────────────────────────────────────────────

def dispatch_call(lead: dict, dry_run: bool = False) -> dict | None:
    phone    = lead.get("raw_phone") or lead.get("phone", "")
    name     = lead["name"]
    biz_type = lead.get("type", "business")
    missing  = lead.get("missing", ["website"])

    if not phone:
        print(f"   ✗ No phone for {name}")
        return None

    pathway  = build_pathway(name, biz_type, missing)
    voicemail = voicemail_message(name, biz_type)

    payload = {
        "phone_number":         phone,
        "from":                 None,        # Bland assigns a number from their pool (free tier)
        "task":                 pathway["global_prompt"],
        "pathway_id":           None,        # We send the pathway inline below
        "voice":                "maya",      # Free voices: maya, ryan, sarah, matt
        # Alternative free voices: "anna", "nat", "derek"
        "reduce_latency":       True,
        "wait_for_greeting":    True,
        "record":               True,
        "amd":                  True,        # Answering machine detection
        "voicemail_message":    voicemail,
        "voicemail_action":     "leave_message",
        "max_duration":         4,           # Minutes
        "webhook":              WEBHOOK_URL,
        "metadata": {
            "lead_id":   str(lead.get("id", "")),
            "biz_name":  name,
            "biz_type":  biz_type,
        },
        # Inline pathway
        "pathway":              pathway,
        # Post-call analysis — Bland will extract these automatically
        "analysis_schema": {
            "outcome": {
                "type":        "string",
                "description": "One of: demo_booked, interested, callback_requested, not_interested, no_answer, voicemail",
            },
            "owner_name": {
                "type":        "string",
                "description": "First name of the owner or manager if mentioned",
            },
            "callback_time": {
                "type":        "string",
                "description": "If callback requested, when they asked to be called",
            },
            "key_notes": {
                "type":        "string",
                "description": "Any important details from the conversation",
            }
        }
    }

    if dry_run:
        print(f"   [DRY RUN] Would call {name} at {phone}")
        print(f"             Missing: {', '.join(missing)}")
        return {"id": "dry-run", "status": "dry_run"}

    try:
        resp = requests.post(
            f"{BLAND_BASE}/calls",
            headers=HEADERS,
            json=payload,
            timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        call_id = data.get("call_id", data.get("id", "?"))
        print(f"   ✓ Calling {name} ({phone}) — Bland call_id: {call_id}")
        return data
    except requests.HTTPError as e:
        print(f"   ✗ Bland API error for {name}: {e.response.status_code} — {e.response.text[:200]}")
        return None
    except Exception as e:
        print(f"   ✗ Error for {name}: {e}")
        return None

# ── CRM helpers ────────────────────────────────────────────────────────────────

def load_leads() -> list:
    if not os.path.exists(CRM_JSON_PATH):
        print(f"No leads file at {CRM_JSON_PATH}. Run scraper_free.py first.")
        return []
    with open(CRM_JSON_PATH) as f:
        return json.load(f)

def save_leads(leads: list):
    with open(CRM_JSON_PATH, "w") as f:
        json.dump(leads, f, indent=2)

# ── Check remaining free minutes ─────────────────────────────────────────────

def check_bland_balance():
    try:
        resp = requests.get(f"{BLAND_BASE}/me", headers=HEADERS, timeout=5)
        if resp.ok:
            data = resp.json()
            minutes_used = data.get("minutes_used", "?")
            minutes_limit = data.get("minutes_limit", 100)
            remaining = minutes_limit - (minutes_used if isinstance(minutes_used, (int,float)) else 0)
            print(f"   Bland free tier: {minutes_used}/{minutes_limit} min used ({remaining:.0f} min remaining)")
            return remaining
    except Exception:
        pass
    return None

# ── Main call queue ───────────────────────────────────────────────────────────

def run_queue(limit: int = None, dry_run: bool = False, min_score: int = 60):
    leads = load_leads()
    queue = [l for l in leads if l.get("status") == "new" and l.get("score", 0) >= min_score]
    queue.sort(key=lambda x: -x.get("score", 0))
    if limit:
        queue = queue[:limit]

    print(f"\n📞 LocalReach — FREE Call Queue (Bland.ai)")
    print(f"   {len(queue)} leads queued (score ≥ {min_score}, highest first)")

    if not dry_run:
        remaining = check_bland_balance()
        if remaining is not None and remaining < len(queue) * 2:
            print(f"\n⚠️  Warning: you may not have enough free minutes for all {len(queue)} calls.")
            print(f"   Estimated usage: {len(queue)*2} min, remaining: {remaining:.0f} min")
            print(f"   Consider using --limit {int(remaining//2)} to stay within free tier.\n")

    lead_map = {str(l.get("id","")): l for l in leads}

    for i, lead in enumerate(queue):
        print(f"\n[{i+1}/{len(queue)}] {lead['name']} (score {lead.get('score',0)})")

        result = dispatch_call(lead, dry_run=dry_run)

        if result and not dry_run:
            for l in leads:
                if str(l.get("id","")) == str(lead.get("id","")):
                    l["status"] = "called"
                    l["bland_call_id"] = result.get("call_id", result.get("id",""))
                    break
            save_leads(leads)

        if i < len(queue) - 1 and not dry_run:
            print(f"   ⏱  Waiting {CALL_GAP_SECONDS}s...")
            time.sleep(CALL_GAP_SECONDS)

    print(f"\n✅ Done. {len(queue)} calls dispatched.")
    print(f"   Outcomes will arrive via webhook to {WEBHOOK_URL}\n")

# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LocalReach Free Agent (Bland.ai)")
    parser.add_argument("--limit",     type=int,  default=None, help="Max calls to place")
    parser.add_argument("--min-score", type=int,  default=60,   help="Min lead score (default 60)")
    parser.add_argument("--dry-run",   action="store_true",     help="Preview without dialing")
    args = parser.parse_args()
    run_queue(limit=args.limit, dry_run=args.dry_run, min_score=args.min_score)
