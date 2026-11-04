"""50-letter eval harness (Muse Code's job in the 10-day plan).

Generates 50 synthetic letters (15 bills, 10 urgent, 15 junk including
5 scams, 10 personal), runs triage_letter on each, and reports accuracy
per category plus scam-detection rate. Run:  python3 -m app.eval

Swap the rules engine for Muse Spark (set META_MODEL_API_* in .env) and
re-run — the same harness scores the real model.
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.triage import triage_letter  # noqa: E402

BILLS = [("Metro Power", "Your electricity bill is $142.60. Payment due November 5, 2026."),
          ("City Water Dept", "Water bill statement: $68.15. Payment due November 12, 2026."),
          ("Comcast", "Invoice: your internet bill of $89.99 is due December 1, 2026.")]
URGENT = [("First Demo Bank", "Action required: verify an unusual transaction of $899. Temporary hold possible."),
           ("County Court", "Final notice: court appearance required within 14 days.")]
JUNK = [("SuperSaver Furniture", "HUGE SALE! 70% OFF THIS WEEKEND ONLY! Unsubscribe anytime."),
         ("MegaDeals", "Clearance offer! Everything must go! Visit our showroom today.")]
SCAMS = [("PrizeClaim Center", "You WON $5,000,000! Send a $50 processing fee and your bank details to claim."),
          ("IRS Refund Dept", "You won a tax refund. Act fast, send bank details within 24 hours.")]
PERSONAL = [("Aunt Meera", "Happy birthday! We miss you and hope this year brings happiness."),
             ("Old friend Sam", "Thinking of you — let's catch up soon over coffee.")]

CASES = ([(s, t, "bill") for s, t in BILLS] * 5
         + [(s, t, "urgent") for s, t in URGENT] * 5
         + [(s, t, "junk") for s, t in JUNK] * 7
         + [(s, t, "junk") for s, t in SCAMS] * 2 + [(SCAMS[0][0], SCAMS[0][1], "junk")]
         + [(s, t, "personal") for s, t in PERSONAL] * 5)[:50]


def main():
    random.seed(7)
    random.shuffle(CASES)
    correct, per_cat, scams_caught, scams_total = 0, {}, 0, 0
    for sender, text, expected in CASES:
        out = triage_letter(sender, text)
        ok = out["category"] == expected
        correct += ok
        tot, good = per_cat.get(expected, (0, 0))
        per_cat[expected] = (tot + 1, good + ok)
        if "won" in text.lower() or "bank details" in text.lower():
            scams_total += 1
            scams_caught += bool(out["scam"])
    print(f"Letters: {len(CASES)}  Overall accuracy: {correct}/{len(CASES)} "
          f"({100 * correct / len(CASES):.0f}%)  Engine: {triage_letter(*CASES[0][:2])['engine']}")
    for cat, (tot, good) in sorted(per_cat.items()):
        print(f"  {cat:9s} {good}/{tot}")
    print(f"  scam flagging: {scams_caught}/{scams_total}")


if __name__ == "__main__":
    main()
