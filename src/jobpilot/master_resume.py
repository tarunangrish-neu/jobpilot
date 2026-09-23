"""Schema and loader for data/master_resume.yaml -- the only source of resume facts.

Scoring embeds the flattened resume; tailoring may only select, reorder, and
rephrase what is in here (tailor/verify.py enforces that).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, model_validator

from . import config


class Bullet(BaseModel):
    id: str
    text: str
    tags: list[str] = Field(default_factory=list)


class Contact(BaseModel):
    name: str
    email: str = ""
    phone: str = ""
    location: str = ""


class Experience(BaseModel):
    company: str
    title: str
    dates: str
    location: str = ""
    bullets: list[Bullet] = Field(default_factory=list)


class Project(BaseModel):
    name: str
    dates: str = ""
    url: str = ""
    bullets: list[Bullet] = Field(default_factory=list)


class Education(BaseModel):
    school: str
    degree: str
    dates: str = ""
    location: str = ""
    details: list[str] = Field(default_factory=list)


class MasterResume(BaseModel):
    contact: Contact
    summary: dict[str, str] = Field(default_factory=dict)  # named variants
    experience: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: dict[str, list[str]] = Field(default_factory=dict)
    education: list[Education] = Field(default_factory=list)
    links: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unique_bullet_ids(self) -> "MasterResume":
        seen: set[str] = set()
        for bullet in self.all_bullets():
            if bullet.id in seen:
                raise ValueError(f"duplicate bullet id '{bullet.id}' in master resume")
            seen.add(bullet.id)
        return self

    def all_bullets(self) -> list[Bullet]:
        out = [b for e in self.experience for b in e.bullets]
        out += [b for p in self.projects for b in p.bullets]
        return out

    def bullet_index(self) -> dict[str, Bullet]:
        return {b.id: b for b in self.all_bullets()}

    def all_skills(self) -> list[str]:
        return [s for group in self.skills.values() for s in group]

    def flatten(self) -> str:
        """Plain-text rendering used for embeddings, prompts, and fact checks."""
        lines: list[str] = []
        for text in self.summary.values():
            lines.append(text)
        for e in self.experience:
            lines.append(f"{e.title} at {e.company} ({e.dates}) {e.location}".strip())
            lines += [f"- {b.text}" for b in e.bullets]
        for p in self.projects:
            lines.append(f"Project: {p.name} {p.dates}".strip())
            lines += [f"- {b.text}" for b in p.bullets]
        for group, items in self.skills.items():
            lines.append(f"{group}: {', '.join(items)}")
        for ed in self.education:
            lines.append(f"{ed.degree}, {ed.school} ({ed.dates})")
            lines += [f"- {d}" for d in ed.details]
        return "\n".join(lines)

    def for_prompt(self) -> str:
        """Resume with bullet ids visible, so the LLM can select by id."""
        lines: list[str] = ["SUMMARY VARIANTS"]
        lines += [f"[{k}] {v}" for k, v in self.summary.items()]
        lines.append("\nEXPERIENCE")
        for e in self.experience:
            lines.append(f"{e.title} | {e.company} | {e.dates}")
            lines += [f"  ({b.id}) {b.text}" for b in e.bullets]
        if self.projects:
            lines.append("\nPROJECTS")
            for p in self.projects:
                lines.append(f"{p.name} | {p.dates}")
                lines += [f"  ({b.id}) {b.text}" for b in p.bullets]
        lines.append("\nSKILLS")
        lines += [f"{g}: {', '.join(s)}" for g, s in self.skills.items()]
        lines.append("\nEDUCATION")
        lines += [f"{ed.degree}, {ed.school} ({ed.dates})" for ed in self.education]
        return "\n".join(lines)


def default_path() -> Path:
    return config.project_root() / "data" / "master_resume.yaml"


def load(path: Optional[Path] = None) -> MasterResume:
    path = path or default_path()
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Run `jobpilot init` to copy the example, then replace it "
            "with your real resume content."
        )
    with path.open("r", encoding="utf-8") as fh:
        return MasterResume.model_validate(yaml.safe_load(fh) or {})
