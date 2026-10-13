"""Prompt templates and fingerprinting."""

from __future__ import annotations

import hashlib

from jobscout.config import PreferencesConfig

PROMPT_VERSION = "1"

SYSTEM_PROMPT = """You are evaluating job postings for a senior software engineer.

Score each posting 0-100 using this rubric:
- 85-100: Squarely in the candidate's field; strong skill and seniority match.
- 65-84: Adjacent or transferable; most core skills match with minor gaps.
- 40-64: General software engineering; some overlap but significant gaps.
- 0-39: Out of field, wrong seniority (people management without IC path), or blocked by preferences.

Rules:
- Judge ONLY from the posting text. Do not invent requirements not stated.
- Senior individual contributor titles fit; pure people-management roles do not.
- If the location is clearly outside the candidate's stated scope, cap the score at 35 and explain.
- Keep "why" to two sentences maximum.

Respond with strict JSON only:
{
  "score": <integer 0-100>,
  "verdict": "<strong|possible|no>",
  "why": "<two sentences max>",
  "seniority_fit": "<brief assessment>",
  "location_fit": "<brief assessment>",
  "matched_skills": ["<skill>", ...],
  "gaps": ["<gap>", ...]
}

Score to verdict mapping: score >= 80 → strong, score >= 50 → possible, else no.
"""


def build_user_prompt(
    profile: str,
    preferences: PreferencesConfig,
    job_title: str,
    job_location: str,
    job_department: str,
    job_description: str,
) -> str:
    desc = job_description[:8000] if job_description else ""
    parts = [
        "# Candidate CV",
        profile.strip(),
        "",
        "# Candidate preferences",
        f"Looking for: {preferences.looking_for}",
        f"Dealbreakers: {preferences.dealbreakers}",
        f"Location scope: {preferences.location_scope}",
        "",
        "# Job posting",
        f"Title: {job_title}",
        f"Location: {job_location}",
        f"Department: {job_department}",
        "",
        desc,
    ]
    return "\n".join(parts)


def prompt_fingerprint(profile: str, preferences: PreferencesConfig) -> str:
    payload = "\x1f".join([
        PROMPT_VERSION,
        SYSTEM_PROMPT,
        profile,
        preferences.looking_for,
        preferences.dealbreakers,
        preferences.location_scope,
    ])
    digest = hashlib.sha256(payload.encode()).hexdigest()[:12]
    return f"{PROMPT_VERSION}-{digest}"
