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

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def next(self):
        tok = self.peek()
        self.pos += 1
        return tok

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
            return ("NOT", self._not())
        if tok == "(":
            self.next()
            node = self._or()
            if self.next() != ")":
                raise BooleanQueryError("missing closing parenthesis")
            return node
        if tok is None or tok == ")":
            raise BooleanQueryError("unexpected end of expression")
        self.next()
        term = tok.strip('"')
        return ("TERM", term)


def parse_boolean_query(query: str):
    """Parse to an AST tuple tree. Empty query → None (matches everything)."""
    if not query or not query.strip():
        return None
    return _Parser(_tokenize(query)).parse()


def evaluate(ast, text: str) -> bool:
    """Evaluate a parsed AST against text (case-insensitive substring terms)."""
    if ast is None:
        return True
    op = ast[0]
    if op == "TERM":
        return ast[1].lower() in text.lower()
    if op == "NOT":
        return not evaluate(ast[1], text)
    if op == "AND":
        return evaluate(ast[1], text) and evaluate(ast[2], text)
    if op == "OR":
        return evaluate(ast[1], text) or evaluate(ast[2], text)
    raise BooleanQueryError(f"unknown node: {op}")


def matches_boolean_query(query: str, text: str) -> bool:
    return evaluate(parse_boolean_query(query), text)
