"""Typed user action, parsed once at the agent boundary.

Every message becomes an Intent with:
  operation  close | reopen | reserve_emergency | keep_direction | no_reopen | rank | why | status | none
  polarity   affirmative | negative        ("Do not close X" / "X is not blocked" are negative)
  mood       imperative | descriptive | question
  direction  the exact direction the user stated (None = whole crossing)
Only an affirmative, non-question action on a mutating operation may lead to a proposal (`may_mutate`).
Commander prohibitions are their own affirmative constraint operations: "Do not reopen X" -> no_reopen,
"Do not reverse X" -> keep_direction. "Do not close X" / "Do not reserve X" are negative and never mutate.
Clarifications carry the Intent unchanged; tools check every mutation against it (fail closed).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, fields

from ..evidence import direction_hint

CLOSE = re.compile(r"\b(blocked|block|closed|close|closure|flooded|flooding|submerged|under water|underwater|impassable|"
                   r"shut|collapsed|water over|overtopp\w*|cut off|not passable|debris|inundated)\b", re.I)
REOPEN = re.compile(r"\b(reopened|re-opened|reopen|re-open|cleared|open again|passable again|back open|open now|is open|"
                    r"has opened|now open)\b", re.I)
RESERVE = re.compile(r"\b(reserve|reserved|dedicate)\b|\b(keep|kept|hold|held|restrict|restricted)\b.*\b(emergency|ambulances?|inbound|responders?)\b|"
                     r"\b(emergency|ambulance)s?\b.*\b(only|access)\b", re.I)
REVERSE = re.compile(r"\b(reverse|reversed|reversal|contraflow)\b|\bkeep (its |the )?direction\b", re.I)
UNSAFE = re.compile(r"\b(unsafe|structural(ly)?|inspection pending|keep (it |\w+ )?closed)\b", re.I)
RANK = re.compile(r"\b(reopen first|clear first|priority|prioriti[sz]e|best|recover\w*|interventions?|options?|"
                  r"rank\w*|plan)\b", re.I)
WHY = re.compile(r"\bwhy\b|\bexplain\b|\bhow (much|important)\b|\bdoes .* matter\b", re.I)
STATUS = re.compile(r"\b(status|summary|summari[sz]e|impact|current|how bad|situation)\b", re.I)
NEG_IMPERATIVE = re.compile(r"^\s*(please\s+)?(do not|don't|dont|never|no need to|stop|avoid)\b|"
                            r"\b(must not|mustn't|should not|shouldn't|do not|don't|dont|never)\s+(\w+\s+){0,1}"
                            r"(close|block|shut|reopen|re-open|open|reserve|keep|dedicate|reverse)\b", re.I)
NEG_DESCRIPTIVE = r"(?:not|n't|no longer|never|isn't|is not|wasn't|aren't|hasn't|has not|haven't)"
NEG_CLOSED = re.compile(rf"\b{NEG_DESCRIPTIVE}\s+(?:\w+\s+){{0,2}}(?:blocked|closed|flooded|submerged|under water|"
                        rf"impassable|shut|cut off|collapsed)\b|\bno (?:longer )?(?:blockage|flooding|water)\b", re.I)
NEG_REOPENED = re.compile(rf"\b{NEG_DESCRIPTIVE}\s+(?:\w+\s+){{0,2}}(?:reopened|re-opened|open|cleared|passable)\b", re.I)
IMPERATIVE_START = re.compile(r"^\s*(please\s+)?(close|shut|block|reopen|re-open|open|reserve|keep|hold|dedicate|"
                              r"rank|first|evaluate|propose)\b", re.I)
QUESTION_START = re.compile(r"^\s*(why|what|which|how|should|could|would|can|is|are|does|did)\b", re.I)

# Verb families per operation (base forms and participles), shared by every negation and modal check.
VERBS = {
    "close": r"close|closed|closing|block|blocked|shut|shutting",
    "reopen": r"reopen|re-open|reopened|re-opened|open|opened|cleared|clear",
    "reserve": r"reserve|reserved|dedicate|dedicated|keep|kept|hold|held|restrict|restricted",
    "reverse": r"reverse|reversed|contraflow|contraflowed",
}
_ANY = "|".join(VERBS.values())
# Prohibitions: imperative or modal negation ("do not reserve", "must not be reserved", "cannot be reopened").
PROHIBIT = r"(?:\b(?:do not|don't|dont|never|stop|avoid|no need to)\b|\b(?:must|should|shall|may|can|will|need|ought to)" \
           r"\s+not\b|\b(?:mustn't|shouldn't|shan't|cannot|can't|won't|needn't)\b)\s+(?:be\s+|have\s+|get\s+)?" \
           r"(?:\w+\s+){0,1}"
# Negated states: "is not reserved", "has not been reopened", "isn't blocked", "no longer flooded".
NEG_STATE = r"\b(?:(?:is|are|was|were|has|have|had|has been|have been|is being)\s+not|isn't|aren't|wasn't|weren't|" \
            r"hasn't|haven't|hadn't|not|no longer)\s+(?:been\s+|being\s+)?(?:\w+\s+){0,1}"
# Affirmative directives phrased with a modal ("must be reserved", "needs to be closed") are commands.
DIRECTIVE = re.compile(r"\b(?:must|should|shall|needs? to|has to|have to|ought to)\s+(?:be\s+)?(?:" + _ANY + r")\b", re.I)


def _prohibits(m: str, fam: str) -> bool:
    return bool(re.search(PROHIBIT + r"(?:" + VERBS[fam] + r")\b", m, re.I))


def _negates(m: str, fam: str) -> bool:
    return bool(re.search(NEG_STATE + r"(?:" + VERBS[fam] + r")\b", m, re.I))


# Hypothetical / investigative requests: read-only analysis, never a proposal by themselves.
REPORTED = re.compile(r"\b(may be|might be|could be|reportedly|apparently|suspected|unconfirmed|reports? say|"
                      r"field team)\b", re.I)
INVESTIGATE = re.compile(r"\b(may be|might be|could be|possibly|reportedly|apparently|suspected|unconfirmed|what if|"
                         r"suppose|supposing|what happens if|find out|investigate|assess|look into|check (whether|if|what)|"
                         r"which neighbou?rhoods?|who is affected|impact of|if .{0,40}\b(were|was|got|gets) "
                         r"(closed|blocked|flooded|shut))\b", re.I)
VULN = re.compile(r"\b(matter(s)? most|most (important|critical|vulnerable)|critical (roads?|links?|crossings?)|"
                  r"vulnerab\w*|weak (points?|links?)|single points? of failure|which roads? (are|is) (critical|key)|"
                  r"bottleneck roads?)\b", re.I)
RECALL = re.compile(r"\b(earlier|previous(ly)?|last time|still (hold|holds|apply|applies|valid|true|stand|stands|current)|"
                    r"that finding|the finding|we found|you found|our investigation|the investigation)\b", re.I)
# A constraint clause starts at one of these markers (at the start of the message or after a boundary) and runs to the
# next clause boundary, so it can come before or after the disruption clause.
CLAUSE_MARK = re.compile(r"(?:^|(?<=[,;.?])\s*|\s)\b(but|while|whilst|however|keeping|keep|and keep|and reserve|and do not reopen)\b",
                         re.I)
CLAUSE = re.compile(r"\s*(?:,|;)?\s*\b(?:but|while|whilst|however|keeping|and keep|and reserve|and do not reopen)\b",
                    re.I)

MUTATING = {"close", "reopen", "reserve_emergency", "keep_direction", "no_reopen"}
CONSTRAINTS = {"reserve_emergency", "keep_direction", "no_reopen"}


@dataclass
class Intent:
    operation: str
    text: str
    polarity: str = "affirmative"
    mood: str = "descriptive"
    direction: str | None = None
    also_rank: bool = False
    primary_text: str | None = None          # investigation: the clause naming the disruption
    aux_constraint: str | None = None        # investigation: a constraint to ASSUME (never proposed by itself)
    aux_text: str | None = None
    aux_direction: str | None = None         # investigation: direction stated INSIDE the constraint clause only

    # ---- derived views (kept for callers and the UI)
    @property
    def constraint_kind(self) -> str | None:
        return self.operation if self.operation in CONSTRAINTS else None

    @property
    def action(self) -> str:
        if self.polarity == "negative":
            return "negated"
        return "constraint" if self.operation in CONSTRAINTS else self.operation

    @property
    def may_mutate(self) -> bool:
        """Fail closed. Constraints are commander instructions: only an affirmative command may propose one; a
        description ("X is reserved") never does. Closure/reopening field reports (descriptive) may lead to a
        proposal, which the human still has to apply."""
        if self.polarity != "affirmative" or self.mood == "question" or self.operation not in MUTATING:
            return False
        if self.operation in CONSTRAINTS:
            return self.mood == "imperative"
        return True

    @property
    def mutating(self) -> bool:          # backwards-compatible name
        return self.may_mutate

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(action=self.action, constraint_kind=self.constraint_kind, may_mutate=self.may_mutate)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Intent":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


def parse_intent(msg: str) -> Intent:
    m = msg.strip()
    d = direction_hint(m)
    question = m.endswith("?") or bool(QUESTION_START.search(m))
    neg_imp = bool(NEG_IMPERATIVE.search(m)) or any(_prohibits(m, f) for f in VERBS)
    mood = "question" if question else "imperative" if (IMPERATIVE_START.search(m) or neg_imp or DIRECTIVE.search(m)) \
        else "descriptive"
    rank_too = bool(re.search(r"\brank\w*\b", m, re.I))

    def it(op, pol="affirmative", md=mood):
        return Intent(op, m, pol, md, d, also_rank=rank_too and op != "rank")

    if VULN.search(m):
        return it("vulnerability", md="question")
    if RECALL.search(m) and (question or re.search(r"\b(recall|remind|what did|does|do|is|are)\b", m, re.I)):
        return it("recall", md="question")
    # an unverified report ("reportedly…", "may be…") is investigated even when phrased as "does it matter?";
    # a plain "why" question about a closure stays an explanation
    if INVESTIGATE.search(m) and (not WHY.search(m) or REPORTED.search(m)):
        return _investigation(m, d, rank_too)
    if WHY.search(m):
        return it("why", md="question")
    # Commander constraints. Negation is resolved per operation BEFORE any affirmative interpretation.
    if RESERVE.search(m):
        if _prohibits(m, "reserve") or (neg_imp and not _prohibits(m, "reopen") and not _prohibits(m, "reverse")):
            return it("reserve_emergency", "negative", "imperative")
        if _negates(m, "reserve"):
            return it("reserve_emergency", "negative", "descriptive")
        return it("reserve_emergency")
    if REVERSE.search(m):
        if _prohibits(m, "reverse") or re.search(r"keep (its |the )?direction", m, re.I):
            return it("keep_direction", md="imperative")
        return it("none")                       # "X is (not) reversed" is a description, never a command
    if REOPEN.search(m) and _prohibits(m, "reopen"):
        return it("no_reopen", md="imperative")
    if UNSAFE.search(m):
        return it("no_reopen")
    if question and RANK.search(m):
        return it("rank", md="question")
    if REOPEN.search(m) and not re.search(r"\breopen first\b", m, re.I):
        if NEG_REOPENED.search(m) or _negates(m, "reopen"):
            return it("reopen", "negative", "descriptive")
        return it("reopen")
    if CLOSE.search(m):
        if _prohibits(m, "close") or (neg_imp and not _negates(m, "reopen")):
            return it("close", "negative", "imperative")
        if NEG_CLOSED.search(m) or _negates(m, "close"):
            return it("close", "negative", "descriptive")
        return it("close")
    if RANK.search(m):
        return it("rank")
    if STATUS.search(m):
        return it("status")
    return it("none")


def split_constraint_clause(m: str) -> tuple[str, str | None, str | None]:
    """Return (disruption text, constraint clause, constraint kind). The constraint clause is the first marked clause
    ("keeping Y reserved…", "while Y stays reserved…") that parses as an affirmative constraint, wherever it sits."""
    for mm in CLAUSE_MARK.finditer(m):
        s = mm.start(1)
        tail = m[mm.end(1):]
        e_m = re.search(r"[,;?]|\.(?:\s|$)", tail)
        e = mm.end(1) + e_m.start() if e_m else len(m)
        clause = m[s:e].strip(" ,.;?")
        sub = parse_intent(re.sub(r"^(?:but|while|whilst|however|and)\s+", "", clause, flags=re.I))
        if sub.operation in CONSTRAINTS and sub.polarity == "affirmative":
            rest = (m[:s].rstrip(" ,;") + " " + m[e:].lstrip(" ,;")).strip(" ,.;") or m
            return rest, clause, sub.operation
    return m, None, None


def _investigation(m: str, d: str | None, rank_too: bool) -> Intent:
    """Split 'X may be flooded; find who is affected, but keep Y for emergency vehicles' into the disruption clause
    and an optional constraint clause that is only ASSUMED for the analysis. Each clause carries its OWN direction:
    a direction written in one clause never applies to the other clause's location."""
    primary, aux_text, aux_kind = split_constraint_clause(m)
    return Intent("investigate", m, "affirmative", "question", direction_hint(primary), also_rank=True,
                  primary_text=primary, aux_constraint=aux_kind, aux_text=aux_text,
                  aux_direction=direction_hint(aux_text) if aux_text else None)
