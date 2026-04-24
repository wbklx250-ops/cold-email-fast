"""
Email Generator Service

Generates unique email addresses from a display name using a deterministic
spec-defined enumeration. NO numbers, NO random suffixes — strictly name-based
patterns only, emitted in a fixed order so `generate_email_variations(...)[:N]`
always returns the same top-N entries for a given persona.

The canonical reference is 'Bruce Johnson @ jegenergy.com' → 100 addresses
(see self-test at bottom).

Ordering (for first=`F`, last=`L`, initials `f`=F[0], `l`=L[0]):

  Block A — singletons (up to 9)
      F, L, f, l, F[:3], F[:2], L[:4], L[:3], L[:2]
      (proper prefixes only — skip any that equal the initial or the full name)

  Block B — first + sep + last, 4 combos × 4 separators (up to 16)
      combos: (F, L), (F, l), (f, L), (f, l)
      seps:   "", ".", "-", "_"

  Block C — last + sep + first, 4 combos × 4 separators (up to 16)
      combos: (L, F), (L, f), (l, F), (l, f)

  Block D — progressive F prefix + full L, × 4 seps
      prefixes in descending length: F[:3], F[:2]

  Block E — full F + progressive L prefix, × 4 seps
      prefixes in descending length: L[:4], L[:3], L[:2]

  Block F — first-initial + progressive L prefix, × 4 seps
  Block G — progressive L prefix + full F, × 4 seps
  Block H — progressive L prefix + first-initial, × 4 seps

  Block I — three triples
      F.L.f,  f.F.L,  L.F.l

If, after all blocks, the caller still needs more than the spec produced
(e.g. a very short persona like "Al Bo"), an a–z letter-suffix fallback is
applied to common base patterns as a last resort.
"""

import logging
import secrets
import string
from typing import List, Dict, Tuple

logger = logging.getLogger(__name__)

# Standard mailbox password used across all mailboxes
MAILBOX_PASSWORD = "#Sendemails1"

# Separators used for compound local parts, in the canonical spec order.
SEPARATORS = ("", ".", "-", "_")


def _proper_prefixes_desc(name: str, max_len: int) -> List[str]:
    """
    Return the 'middle' prefixes of `name` in descending length order,
    excluding the one-character initial and the full name.

    Example:
        _proper_prefixes_desc("bruce", 3)   -> ["bru", "br"]
        _proper_prefixes_desc("johnson", 4) -> ["john", "joh", "jo"]
        _proper_prefixes_desc("al", 4)      -> []          # too short
        _proper_prefixes_desc("bob", 4)     -> ["bo"]
    """
    out: List[str] = []
    # We want lengths 2..min(max_len, len(name)-1), emitted high-to-low.
    upper = min(max_len, len(name) - 1)
    for n in range(upper, 1, -1):
        out.append(name[:n])
    return out


def _add(out: List[str], seen: set, local: str, domain: str) -> None:
    """Append f'{local}@{domain}' to `out` if not already seen."""
    if not local:
        return
    addr = f"{local}@{domain}"
    if addr in seen:
        return
    seen.add(addr)
    out.append(addr)


def generate_email_variations(
    first_name: str,
    last_name: str,
    domain: str,
    count: int = 50,
) -> List[Dict[str, str]]:
    """
    Generate unique email variations from a persona name in spec order.

    Returns at most `count` entries. If the spec + fallback still can't
    produce `count` unique addresses (e.g. extremely short name), fewer
    entries are returned.
    """
    first = (first_name or "").strip().lower()
    last = (last_name or "").strip().lower()

    if not first or not last:
        raise ValueError(
            f"generate_email_variations: both first_name and last_name are required "
            f"(got first={first_name!r} last={last_name!r})"
        )

    f, l = first[0], last[0]
    first_prefixes_desc = _proper_prefixes_desc(first, max_len=3)  # e.g. [bru, br]
    last_prefixes_desc = _proper_prefixes_desc(last, max_len=4)    # e.g. [john, joh, jo]

    emails: List[str] = []
    seen: set = set()

    # ----------------------------------------------------------------------
    # Block A — singletons
    # ----------------------------------------------------------------------
    _add(emails, seen, first, domain)
    _add(emails, seen, last, domain)
    _add(emails, seen, f, domain)
    _add(emails, seen, l, domain)
    for p in first_prefixes_desc:
        _add(emails, seen, p, domain)
    for p in last_prefixes_desc:
        _add(emails, seen, p, domain)

    # ----------------------------------------------------------------------
    # Block B — first+last, 4 combos × 4 separators
    # ----------------------------------------------------------------------
    for a, b in ((first, last), (first, l), (f, last), (f, l)):
        for sep in SEPARATORS:
            _add(emails, seen, f"{a}{sep}{b}", domain)

    # ----------------------------------------------------------------------
    # Block C — last+first, 4 combos × 4 separators
    # ----------------------------------------------------------------------
    for a, b in ((last, first), (last, f), (l, first), (l, f)):
        for sep in SEPARATORS:
            _add(emails, seen, f"{a}{sep}{b}", domain)

    # ----------------------------------------------------------------------
    # Block D — progressive first prefix + full last (× 4 seps)
    # ----------------------------------------------------------------------
    for p in first_prefixes_desc:
        for sep in SEPARATORS:
            _add(emails, seen, f"{p}{sep}{last}", domain)

    # ----------------------------------------------------------------------
    # Block E — full first + progressive last prefix (× 4 seps)
    # ----------------------------------------------------------------------
    for p in last_prefixes_desc:
        for sep in SEPARATORS:
            _add(emails, seen, f"{first}{sep}{p}", domain)

    # ----------------------------------------------------------------------
    # Block F — first initial + progressive last prefix (× 4 seps)
    # ----------------------------------------------------------------------
    for p in last_prefixes_desc:
        for sep in SEPARATORS:
            _add(emails, seen, f"{f}{sep}{p}", domain)

    # ----------------------------------------------------------------------
    # Block G — progressive last prefix + full first (× 4 seps)
    # ----------------------------------------------------------------------
    for p in last_prefixes_desc:
        for sep in SEPARATORS:
            _add(emails, seen, f"{p}{sep}{first}", domain)

    # ----------------------------------------------------------------------
    # Block H — progressive last prefix + first initial (× 4 seps)
    # ----------------------------------------------------------------------
    for p in last_prefixes_desc:
        for sep in SEPARATORS:
            _add(emails, seen, f"{p}{sep}{f}", domain)

    # ----------------------------------------------------------------------
    # Block I — triples
    # ----------------------------------------------------------------------
    _add(emails, seen, f"{first}.{last}.{f}", domain)
    _add(emails, seen, f"{f}.{first}.{last}", domain)
    _add(emails, seen, f"{last}.{first}.{l}", domain)

    # ----------------------------------------------------------------------
    # Fallback — a-z letter suffixes on common base patterns. Only used when
    # the spec enumeration produces fewer unique addresses than the caller
    # asked for (typically very short personas). Kept as a last resort so
    # short names don't fail outright.
    # ----------------------------------------------------------------------
    if len(emails) < count:
        base_patterns = [
            f"{first}.{last}",
            f"{first}{last}",
            f"{f}{last}",
            f"{last}.{first}",
            f"{last}{first}",
            f"{first}_{last}",
            f"{first}-{last}",
        ]
        logger.debug(
            "email_generator: spec produced %s addresses, requested %s — "
            "falling back to a-z suffix patterns",
            len(emails), count,
        )
        for suffix in string.ascii_lowercase:
            if len(emails) >= count:
                break
            for base in base_patterns:
                if len(emails) >= count:
                    break
                _add(emails, seen, f"{base}{suffix}", domain)

    # Trim to requested count (may be less if both spec and fallback exhausted)
    emails = emails[:count]

    display_name = f"{first_name} {last_name}".strip()
    return [{"email": email, "display_name": display_name} for email in emails]


def generate_password(length: int = 12) -> str:
    """
    Generate a secure password.

    Requirements:
    - At least one lowercase letter
    - At least one uppercase letter
    - At least one digit
    - At least one special character
    - No ambiguous characters (0, O, l, 1, I)
    - Compliant with M365 password policy
    """
    # Character sets (excluding ambiguous characters)
    lowercase = "abcdefghjkmnpqrstuvwxyz"  # no l
    uppercase = "ABCDEFGHJKMNPQRSTUVWXYZ"  # no I, O
    digits = "23456789"  # no 0, 1
    special = "!@#$%^&*()-_=+"

    # Ensure at least one of each required type
    password = [
        secrets.choice(lowercase),
        secrets.choice(uppercase),
        secrets.choice(digits),
        secrets.choice(special),
    ]

    # Fill remaining length with random mix
    all_chars = lowercase + uppercase + digits + special
    password.extend(secrets.choice(all_chars) for _ in range(length - 4))

    # Shuffle to randomize position of required characters
    password_list = list(password)
    secrets.SystemRandom().shuffle(password_list)

    return "".join(password_list)


def parse_display_name(display_name: str) -> Tuple[str, str]:
    """
    Parse display name into first and last name.

    Args:
        display_name: Full name like "Jack Zuvelek" or "Mary Jane Smith"

    Returns:
        Tuple of (first_name, last_name) in original casing.
        For "Mary Jane Smith" → ("Mary Jane", "Smith") so that the last token
        is always treated as the surname (matches the per-domain persona
        convention in validation_service).
    """
    parts = display_name.strip().split()

    if len(parts) < 2:
        raise ValueError(f"Display name must have first and last name: '{display_name}'")

    last = parts[-1]
    first = " ".join(parts[:-1])

    return first, last


def generate_emails_for_domain(
    display_name: str,
    domain: str,
    count: int = 50,
) -> List[Dict[str, str]]:
    """
    Generate email addresses for a domain.

    Args:
        display_name: Full name like "Jack Zuvelek"
        domain: Domain like "loancatermail13.info"
        count: Max number of emails to generate (default 50)

    Returns:
        List of dicts with keys: email, display_name, password, local_part
    """
    first, last = parse_display_name(display_name)

    variations = generate_email_variations(first, last, domain, count)
    emails = []

    for variation in variations:
        email = variation["email"]
        local_part = email.split("@")[0]
        # Use standard mailbox password for all mailboxes
        password = MAILBOX_PASSWORD

        emails.append(
            {
                "email": email,
                "display_name": variation["display_name"],
                "password": password,
                "local_part": local_part,
            }
        )

    return emails


def generate_email_addresses(
    first_name: str,
    last_name: str,
    domain: str,
    count: int = 50,
) -> List[Dict[str, str]]:
    """
    Backward compatibility wrapper for orchestrator.py.
    """
    display_name = f"{first_name} {last_name}"
    return generate_emails_for_domain(display_name, domain, count)


def generate_emails_for_batch(
    display_name: str,
    domains: List[str],
    emails_per_domain: int = 50,
) -> List[Dict[str, str]]:
    """
    Generate emails for multiple domains in a batch.
    """
    all_emails = []

    for domain in domains:
        domain_emails = generate_emails_for_domain(
            display_name=display_name,
            domain=domain,
            count=emails_per_domain,
        )
        all_emails.extend(domain_emails)

    return all_emails


# ============================================================================
# SELF-TEST — asserts Bruce Johnson @ jegenergy.com matches the reference spec
# ============================================================================

EXPECTED_BRUCE_JOHNSON = [
    "bruce@jegenergy.com",
    "johnson@jegenergy.com",
    "b@jegenergy.com",
    "j@jegenergy.com",
    "bru@jegenergy.com",
    "br@jegenergy.com",
    "john@jegenergy.com",
    "joh@jegenergy.com",
    "jo@jegenergy.com",
    "brucejohnson@jegenergy.com",
    "bruce.johnson@jegenergy.com",
    "bruce-johnson@jegenergy.com",
    "bruce_johnson@jegenergy.com",
    "brucej@jegenergy.com",
    "bruce.j@jegenergy.com",
    "bruce-j@jegenergy.com",
    "bruce_j@jegenergy.com",
    "bjohnson@jegenergy.com",
    "b.johnson@jegenergy.com",
    "b-johnson@jegenergy.com",
    "b_johnson@jegenergy.com",
    "bj@jegenergy.com",
    "b.j@jegenergy.com",
    "b-j@jegenergy.com",
    "b_j@jegenergy.com",
    "johnsonbruce@jegenergy.com",
    "johnson.bruce@jegenergy.com",
    "johnson-bruce@jegenergy.com",
    "johnson_bruce@jegenergy.com",
    "johnsonb@jegenergy.com",
    "johnson.b@jegenergy.com",
    "johnson-b@jegenergy.com",
    "johnson_b@jegenergy.com",
    "jbruce@jegenergy.com",
    "j.bruce@jegenergy.com",
    "j-bruce@jegenergy.com",
    "j_bruce@jegenergy.com",
    "jb@jegenergy.com",
    "j.b@jegenergy.com",
    "j-b@jegenergy.com",
    "j_b@jegenergy.com",
    "brujohnson@jegenergy.com",
    "bru.johnson@jegenergy.com",
    "bru-johnson@jegenergy.com",
    "bru_johnson@jegenergy.com",
    "brjohnson@jegenergy.com",
    "br.johnson@jegenergy.com",
    "br-johnson@jegenergy.com",
    "br_johnson@jegenergy.com",
    "brucejohn@jegenergy.com",
    "bruce.john@jegenergy.com",
    "bruce-john@jegenergy.com",
    "bruce_john@jegenergy.com",
    "brucejoh@jegenergy.com",
    "bruce.joh@jegenergy.com",
    "bruce-joh@jegenergy.com",
    "bruce_joh@jegenergy.com",
    "brucejo@jegenergy.com",
    "bruce.jo@jegenergy.com",
    "bruce-jo@jegenergy.com",
    "bruce_jo@jegenergy.com",
    "bjohn@jegenergy.com",
    "b.john@jegenergy.com",
    "b-john@jegenergy.com",
    "b_john@jegenergy.com",
    "bjoh@jegenergy.com",
    "b.joh@jegenergy.com",
    "b-joh@jegenergy.com",
    "b_joh@jegenergy.com",
    "bjo@jegenergy.com",
    "b.jo@jegenergy.com",
    "b-jo@jegenergy.com",
    "b_jo@jegenergy.com",
    "johnbruce@jegenergy.com",
    "john.bruce@jegenergy.com",
    "john-bruce@jegenergy.com",
    "john_bruce@jegenergy.com",
    "johbruce@jegenergy.com",
    "joh.bruce@jegenergy.com",
    "joh-bruce@jegenergy.com",
    "joh_bruce@jegenergy.com",
    "jobruce@jegenergy.com",
    "jo.bruce@jegenergy.com",
    "jo-bruce@jegenergy.com",
    "jo_bruce@jegenergy.com",
    "johnb@jegenergy.com",
    "john.b@jegenergy.com",
    "john-b@jegenergy.com",
    "john_b@jegenergy.com",
    "johb@jegenergy.com",
    "joh.b@jegenergy.com",
    "joh-b@jegenergy.com",
    "joh_b@jegenergy.com",
    "job@jegenergy.com",
    "jo.b@jegenergy.com",
    "jo-b@jegenergy.com",
    "jo_b@jegenergy.com",
    "bruce.johnson.b@jegenergy.com",
    "b.bruce.johnson@jegenergy.com",
    "johnson.bruce.j@jegenergy.com",
]


def _self_test() -> int:
    """Run the Bruce Johnson spec check. Returns 0 on pass, 1 on failure."""
    result = generate_emails_for_domain("Bruce Johnson", "jegenergy.com", count=100)
    actual = [e["email"] for e in result]

    ok = True
    if len(actual) != len(EXPECTED_BRUCE_JOHNSON):
        print(f"✗ length mismatch: got {len(actual)}, expected {len(EXPECTED_BRUCE_JOHNSON)}")
        ok = False

    for i, (got, want) in enumerate(zip(actual, EXPECTED_BRUCE_JOHNSON)):
        if got != want:
            print(f"✗ index {i}: got {got!r}, expected {want!r}")
            ok = False

    # Catch extra entries on either side
    if len(actual) > len(EXPECTED_BRUCE_JOHNSON):
        for i in range(len(EXPECTED_BRUCE_JOHNSON), len(actual)):
            print(f"✗ extra at index {i}: {actual[i]!r}")
    if len(EXPECTED_BRUCE_JOHNSON) > len(actual):
        for i in range(len(actual), len(EXPECTED_BRUCE_JOHNSON)):
            print(f"✗ missing at index {i}: expected {EXPECTED_BRUCE_JOHNSON[i]!r}")

    if ok:
        print(f"✓ Bruce Johnson self-test passed ({len(actual)} addresses match spec)")
        return 0
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(_self_test())
