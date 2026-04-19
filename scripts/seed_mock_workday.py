"""Generate 100 fake employees for services/mock_workday.

Deterministic via a fixed seed so tests + demos line up.

Run: uv run python scripts/seed_mock_workday.py
"""

from __future__ import annotations

import json
import random
from pathlib import Path

FIRST = [
    "Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Quinn", "Avery",
    "Jamie", "Parker", "Rowan", "Skyler", "Hayden", "Reese", "Emerson",
    "Finley", "Dakota", "Peyton", "Harper", "Kai",
]
LAST = [
    "Nguyen", "Smith", "Patel", "Garcia", "Kim", "Brown", "Johnson", "Lopez",
    "Singh", "Martinez", "Chen", "Wilson", "Taylor", "Davis", "Lee",
    "Rodriguez", "Anderson", "Wright", "Walker", "Young",
]
TITLES = [
    "Software Engineer", "Staff Engineer", "Engineering Manager",
    "Product Manager", "Designer", "Data Scientist", "Security Engineer",
    "SRE", "Solutions Architect", "Director", "QA Engineer",
]
DEPTS = [
    "Platform", "Infra", "Product", "Design", "Data", "Security",
    "Customer Success", "Sales", "Finance", "People",
]


def generate(n: int = 100, seed: int = 42) -> list[dict]:
    rng = random.Random(seed)
    out = []
    for i in range(1, n + 1):
        fn = rng.choice(FIRST)
        ln = rng.choice(LAST)
        out.append({
            "employee_id": f"E-{1000 + i}",
            "handle": f"{fn.lower()}{ln.lower()}{i}",
            "name": f"{fn} {ln}",
            "title": rng.choice(TITLES),
            "department": rng.choice(DEPTS),
            "email": f"{fn.lower()}.{ln.lower()}@example.com",
        })
    return out


def main() -> None:
    root = Path(__file__).resolve().parents[1] / "services" / "mock_workday"
    out = generate()
    (root / "employees.json").write_text(json.dumps(out, indent=2))
    print(f"wrote {len(out)} employees -> {root / 'employees.json'}")


if __name__ == "__main__":
    main()
