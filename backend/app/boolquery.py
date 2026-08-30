"""Boolean search query parser/evaluator.

Grammar (case-insensitive keywords):
    expr    := or_expr
    or_expr := and_expr (OR and_expr)*
    and_expr:= not_expr (AND not_expr)*     -- juxtaposition also implies AND
    not_expr:= NOT not_expr | '(' expr ')' | term
    term    := word | "quoted phrase"

Example: (React OR Next.js) AND (NOT WordPress)
"""
import re

_TOKEN_RE = re.compile(r'\s*(\(|\)|\bAND\b|\bOR\b|\bNOT\b|"[^"]+"|[^\s()"]+)', re.IGNORECASE)

MAX_QUERY_LENGTH = 1000  # chars — query text is user-supplied
MAX_DEPTH = 32           # paren/NOT nesting — bounds parser & evaluator recursion


class BooleanQueryError(ValueError):
    pass


def _tokenize(query: str) -> list[str]:
    tokens, pos = [], 0
    for m in _TOKEN_RE.finditer(query):
        tokens.append(m.group(1))
        pos = m.end()
    if query[pos:].strip():
        raise BooleanQueryError(f"unexpected trailing input: {query[pos:]!r}")
    return tokens


class _Parser:
    def __init__(self, tokens: list[str]):
        self.tokens = tokens
        self.pos = 0
        self.depth = 0

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def next(self):
        tok = self.peek()
        self.pos += 1
        return tok

    def _descend(self, rule):
        """Track nesting depth so hostile queries can't blow the stack."""
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise BooleanQueryError(f"query nesting too deep (max {MAX_DEPTH})")
        try:
            return rule()
        finally:
            self.depth -= 1

    def parse(self):
        node = self._or()
        if self.peek() is not None:
            raise BooleanQueryError(f"unexpected token: {self.peek()!r}")
        return node

    def _or(self):
        node = self._and()
        while (self.peek() or "").upper() == "OR":
            self.next()
            node = ("OR", node, self._and())
        return node

    def _and(self):
        node = self._not()
        while True:
            tok = self.peek()
            if tok is None or tok == ")" or tok.upper() == "OR":
                return node
            if tok.upper() == "AND":
                self.next()
            # juxtaposition = implicit AND
            node = ("AND", node, self._not())

    def _not(self):
        tok = self.peek()
        if tok is not None and tok.upper() == "NOT":
            self.next()
            return ("NOT", self._descend(self._not))
        if tok == "(":
            self.next()
            node = self._descend(self._or)
            if self.next() != ")":
                raise BooleanQueryError("missing closing parenthesis")
            return node
        if tok is None or tok == ")":
            raise BooleanQueryError("unexpected end of expression")
        self.next()
        if tok.startswith('"') and tok.endswith('"') and len(tok) >= 2:
            return ("TERM", tok[1:-1], True)   # quoted phrase: substring semantics
        return ("TERM", tok, False)            # single word: word-boundary match


def parse_boolean_query(query: str):
    """Parse to an AST tuple tree. Empty query → None (matches everything)."""
    if not query or not query.strip():
        return None
    if len(query) > MAX_QUERY_LENGTH:
        raise BooleanQueryError(f"query too long ({len(query)} > {MAX_QUERY_LENGTH} chars)")
    return _Parser(_tokenize(query)).parse()


def evaluate(ast, text: str) -> bool:
    """Evaluate a parsed AST against text.

    Quoted/phrase terms keep case-insensitive substring semantics; single-word
    terms match on word boundaries ("ai" does not match "said").
    """
    if ast is None:
        return True
    op = ast[0]
    if op == "TERM":
        term, phrase = ast[1], ast[2]
        if phrase:
            return term.lower() in text.lower()
        return bool(re.search(r"\b" + re.escape(term) + r"\b", text, re.IGNORECASE))
    if op == "NOT":
        return not evaluate(ast[1], text)
    if op == "AND":
        return evaluate(ast[1], text) and evaluate(ast[2], text)
    if op == "OR":
        return evaluate(ast[1], text) or evaluate(ast[2], text)
    raise BooleanQueryError(f"unknown node: {op}")


def matches_boolean_query(query: str, text: str) -> bool:
    return evaluate(parse_boolean_query(query), text)
