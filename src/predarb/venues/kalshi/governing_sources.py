"""Kalshi's citation styles, and the documents its contracts incorporate.

This is the venue-specific half of incorporation: the semantics layer knows what
a dependency *is*, and this module knows that Kalshi contract terms say things
like "pursuant to Rule 6.3(b) in the Rulebook".

Why the Rulebook is fetched at all
----------------------------------
Every Kalshi contract-terms document examined in the Phase-1 acceptance pass
carried this sentence verbatim:

    "Before Settlement, Kalshi may, at its sole discretion, initiate the Market
    Outcome Review Process pursuant to Rule 6.3(d) of the Rulebook. If an
    Expiration Value cannot be determined on the Expiration Date, Kalshi has the
    right to determine payouts pursuant to Rule 6.3(b) in the Rulebook."

That is the payout rule for an entire class of terminal states, and it is in a
document the Step 8 capture never fetched.

How the Rulebook binds
----------------------
Settled, and by the Member Agreement's own words rather than by inference. A
Member is bound to "the Kalshi rules (as supplemented or amended from time to
time, the 'Kalshi Rulebook')" and to "the Kalshi Rulebook, as now existing and
as hereafter duly amended from time to time". The Rulebook also governs over the
Member Agreement in the event of conflict. So the *edition* question has an
answer: ``AS_AMENDED_FROM_TIME_TO_TIME``.

That is a different question from what a written citation points at, and
answering the first does not answer the second -- see below.

A caution recorded, not resolved
--------------------------------
The rule numbers cited by the contract terms do **not** line up with the current
Rulebook. In v1.29, the provision that determines payouts when an Expiration
Value cannot be determined is Rule 6.3(c), and the Market Outcome Review Process
is Rule 6.3(f); v1.29's actual 6.3(b) is the Scalar Contract rule and its 6.3(d)
is settlement-date mechanics. The terms therefore cite an earlier numbering.

Worse than a renumbering: Rule 6.3 is *structurally identical* in v1.18 (July
2025) and v1.29. 6.3(b) was already the Scalar Contract rule when the
neighbouring CRIMECHARGE product was certified in July 2025, so the citation did
not match the contemporaneous Rulebook either. Dynamic binding does not repair
that; it only means the target keeps moving.

Nothing here silently maps one numbering onto the other. The citation is
recorded exactly as written and the retrieved Rulebook records its own version,
so a reviewer sees both and can resolve the mismatch themselves. A scanner that
"helpfully" renumbered citations would be asserting a legal interpretation.

The canonical Rulebook page (``kalshi.com/regulatory/rulebook``) answers HTTP 429
to automated fetches, as does the Member Agreement, so the versioned object in
Kalshi's public S3 bucket is used instead and the URL actually fetched is what
gets recorded.
"""

from __future__ import annotations

import re
from typing import Final

from predarb.semantics.dependency import VersionBinding
from predarb.semantics.incorporation import CitationGrammar, CitationPattern

__all__ = [
    "CFTC_REGULATIONS",
    "EXCHANGE_RULEBOOK",
    "GOVERNING_SOURCE_NAMES",
    "GOVERNING_SOURCE_URLS",
    "KALSHI_CITATION_GRAMMAR",
    "MEMBER_AGREEMENT",
]

EXCHANGE_RULEBOOK: Final = "exchange_rulebook"
MEMBER_AGREEMENT: Final = "member_agreement"
CFTC_REGULATIONS: Final = "cftc_regulations"

GOVERNING_SOURCE_URLS: Final[dict[str, str]] = {
    # The versioned object in Kalshi's public documents bucket. The canonical
    # page under kalshi.com answers 429 to automated requests, and a 429 body is
    # an error page, not the Rulebook -- hashing it would record a fetch failure
    # as though it were evidence.
    EXCHANGE_RULEBOOK: (
        "https://kalshi-public-docs.s3.amazonaws.com/regulatory/rulebook/"
        "Kalshi%20DCM%20Rulebook%20v.1.29.pdf"
    ),
    MEMBER_AGREEMENT: "https://kalshi.com/docs/kalshi-member-agreement.pdf",
}

GOVERNING_SOURCE_NAMES: Final[tuple[str, ...]] = (EXCHANGE_RULEBOOK, MEMBER_AGREEMENT)
"""Venue-level governing documents, in the dependency graph in their own right.

The Member Agreement is here rather than summarised in code because it is what
establishes that the Rulebook binds dynamically. Holding it as evidence means a
change to *that* document -- including a change to the amendment language --
becomes visible drift instead of a stale comment in this file.
"""

# "Rule 6.3(b)", "Rule 7.1", "Rule 2.17(a)" -- the citation is kept exactly as
# written, including the numbering, because the numbering is itself evidence
# about which edition of the Rulebook the terms were drafted against.
#
# The lookbehinds keep federal regulations out. A Kalshi filing writes "CFTC
# Regulation 40.2(a)" and "Commission Rule 40.2"; those are Part 40
# self-certification procedure, not Rulebook rules, and attributing them to the
# Rulebook produced a BROKEN_REFERENCE against a document that never contained
# them. The Rulebook's own chapters run 1-13, so a bare "Rule 38.x", "Rule
# 40.x", "Rule 155.x" or "Rule 156.x" names a CFTC part that the Rulebook has
# never contained; those parts are excluded here and matched below instead.
_RULE_CITATION = re.compile(
    r"(?<!CFTC )(?<!Commission )"
    r"(?P<ref>Rule\s+(?!38\.|40\.|155\.|156\.)\d+(?:\.\d+)+(?:\s*\([a-z0-9]+\))?)",
    re.IGNORECASE,
)

# Federal law, cited by Kalshi filings for how a contract came to be listed --
# not for what it pays. Part 40 is product and rule self-certification.
_CFTC_REGULATION_CITATION = re.compile(
    r"(?P<ref>"
    r"(?:CFTC|Commission)\s+(?:Regulation|Rule)\s+\d+(?:\.\d+)+(?:\s*\([a-z0-9]+\))?"
    r"|Regulation\s+\d+(?:\.\d+)+(?:\s*\([a-z0-9]+\))?"
    r"|Section\s+40\.\d+(?:\s*\([a-z0-9]+\))?"
    r"|Rule\s+(?:38|40|155|156)\.\d+(?:\s*\([a-z0-9]+\))?"
    r")",
    re.IGNORECASE,
)

# A bare incorporation with no rule number: "subject to the Rulebook". Distinct
# from a numbered citation and recorded separately, because it incorporates the
# whole document rather than one provision.
_RULEBOOK_CITATION = re.compile(
    r"(?P<ref>(?:the\s+)?(?:KalshiEX\s+|Kalshi\s+|Exchange\s+)?(?:DCM\s+)?Rulebook)",
    re.IGNORECASE,
)

_MEMBER_AGREEMENT_CITATION = re.compile(
    r"(?P<ref>(?:the\s+)?(?:Kalshi\s+)?Member(?:ship)?\s+Agreement)",
    re.IGNORECASE,
)

KALSHI_CITATION_GRAMMAR: Final = CitationGrammar(
    name="kalshi/1",
    patterns=(
        CitationPattern(
            pattern=_RULE_CITATION,
            source_name=EXCHANGE_RULEBOOK,
            source_url=GOVERNING_SOURCE_URLS[EXCHANGE_RULEBOOK],
            # Established by the Member Agreement, quoted in this module's
            # docstring. This settles only *which edition* governs; it says
            # nothing about what "6.3(b)" points at, which stays UNKNOWN on the
            # separate ReferenceResolution axis until authority establishes it.
            version_binding=VersionBinding.AS_AMENDED_FROM_TIME_TO_TIME,
        ),
        CitationPattern(
            pattern=_RULEBOOK_CITATION,
            source_name=EXCHANGE_RULEBOOK,
            source_url=GOVERNING_SOURCE_URLS[EXCHANGE_RULEBOOK],
            version_binding=VersionBinding.AS_AMENDED_FROM_TIME_TO_TIME,
        ),
        CitationPattern(
            pattern=_CFTC_REGULATION_CITATION,
            source_name=CFTC_REGULATIONS,
            source_url="https://www.ecfr.gov/current/title-17/chapter-I/part-40",
            # The CEA and its regulations are federal law; they are not
            # "amended from time to time" by the exchange, and a Kalshi
            # amendment cannot move them.
            version_binding=VersionBinding.AS_AMENDED_FROM_TIME_TO_TIME,
        ),
        CitationPattern(
            pattern=_MEMBER_AGREEMENT_CITATION,
            source_name=MEMBER_AGREEMENT,
            source_url=GOVERNING_SOURCE_URLS[MEMBER_AGREEMENT],
            version_binding=VersionBinding.UNKNOWN,
        ),
    ),
)
