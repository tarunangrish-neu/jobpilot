"""Anti-fabrication check for everything the LLM writes about the candidate.

A rephrased bullet may only restate its original. Concretely, every
  * number (digits, or number words like "doubled" / "million"),
  * technology (known tech terms and every skill in the master resume), and
  * proper noun (capitalized mid-sentence, internal caps like "PostgreSQL",
    or letter+digit tokens like "EC2")
must already be in the source. Numbers and technologies must be in the
*original bullet* -- moving "Kubernetes" from one job onto another is a
fabricated claim even though the word exists elsewhere. Other proper nouns
may come from anywhere in the master resume.

Anything new -> the rephrase is rejected and the original bullet is used.
The check is deliberately conservative: a false rejection costs a bit of
polish, a false accept puts a lie on a resume.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..master_resume import MasterResume

NUMBER_WORDS = {
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "fifteen", "twenty", "thirty", "forty", "fifty", "hundred",
    "hundreds", "thousand", "thousands", "million", "millions", "billion", "billions",
    "dozen", "dozens", "double", "doubled", "doubling", "triple", "tripled", "tripling",
    "quadrupled", "tenfold", "twofold", "threefold", "half", "halved", "percent", "twice",
}

# Lowercase tech terms caught even when written in lowercase. Words that are
# also ordinary English (go, rest, spring, swift, ray) are left out; they are
# still caught when capitalized mid-sentence.
TECH_TERMS = {
    "python", "golang", "rust", "java", "kotlin", "scala", "javascript", "typescript",
    "react", "nodejs", "node.js", "django", "flask", "fastapi", "kubernetes", "k8s",
    "docker", "terraform", "ansible", "pulumi", "aws", "gcp", "azure", "kafka", "rabbitmq",
    "redis", "postgres", "postgresql", "mysql", "sqlite", "mongodb", "cassandra", "dynamodb",
    "elasticsearch", "opensearch", "clickhouse", "spark", "hadoop", "airflow", "dbt",
    "snowflake", "bigquery", "pytorch", "tensorflow", "jax", "cuda", "triton", "vllm",
    "onnx", "tensorrt", "graphql", "grpc", "protobuf", "linux", "c++", "c#", "ruby",
    "php", "solidity", "ethereum", "solana", "bitcoin", "evm", "llm", "llms", "rag",
    "langchain", "prometheus", "grafana", "datadog", "opentelemetry", "jenkins", "circleci",
    "sql", "nosql", "s3", "ec2", "kinesis", "sqs", "sns", "mlflow", "kubeflow", "haskell",
    "elixir", "erlang", "clojure", "zig", "wasm", "webassembly", "nginx", "envoy", "istio",
    "helm", "argocd", "vault", "consul", "nomad", "bazel", "temporal", "celery", "pandas",
    "numpy", "kdb", "flink", "pulsar", "nats", "zookeeper", "etcd", "raft", "paxos",
    # Frameworks and tools a posting names that a letter must not claim unless the resume does.
    "angular", "angularjs", "vue", "vuejs", "svelte", "nextjs", "next.js", "nuxt", "redux", "jquery",
    "webpack", "vite", "tailwind", "sass", "hibernate", "dotnet", ".net", "asp.net", "laravel", "rails",
    "flutter", "xamarin", "ios", "android", "objective-c", "tableau", "looker", "powerbi", "snowpark",
    "databricks", "sagemaker", "vertexai", "bedrock", "presto", "trino", "hive", "hbase", "neo4j",
    "couchbase", "memcached", "openshift", "vmware", "selenium", "cypress", "playwright", "jest",
    "junit", "pytest", "mocha", "jira", "gitlab", "github", "bitbucket", "matlab", "perl", "bash",
    "powershell", "cobol", "fortran", "unity", "splunk", "newrelic", "sentry", "pagerduty", "okta",
    "auth0", "keycloak", "stripe", "twilio", "salesforce", "servicenow", "mulesoft", "informatica",
}

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+#]*[A-Za-z0-9+#]|[A-Za-z0-9]")
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
# A token right after one of these (or at the very start) begins a sentence/clause.
_INITIAL_AFTER = set(".!?:;•-–—\"'(")


_DIGIT_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
    "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "fifteen": "15",
    "twenty": "20", "thirty": "30", "forty": "40", "fifty": "50", "hundred": "100",
}
_MAGNITUDE = {"k": "k", "thousand": "k", "m": "m", "million": "m", "b": "b", "billion": "b"}
_SCALED_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(k|m|b|thousand|million|billion)\b", re.I)


def numbers_in(text: str) -> set[str]:
    """Numeric claims in canonical form, so "six" == "6" and "2 million" == "2M"."""
    low = text.lower()
    nums = {n.replace(",", "") for n in _NUMBER_RE.findall(low)}
    scaled = {f"{n}{_MAGNITUDE[mag]}" for n, mag in _SCALED_RE.findall(low)}
    attached = {mag for _, mag in _SCALED_RE.findall(low)}
    words: set[str] = set()
    for w in re.findall(r"[a-z]+", low):
        if w in _DIGIT_WORDS:
            words.add(_DIGIT_WORDS[w])
        elif w in NUMBER_WORDS and w not in attached:
            words.add(w)
    return nums | scaled | words


def _tokens(text: str) -> list[tuple[str, bool]]:
    """(token, is_sentence_initial) pairs."""
    out: list[tuple[str, bool]] = []
    for m in _TOKEN_RE.finditer(text):
        prefix = text[: m.start()].rstrip()
        out.append((m.group(0), not prefix or prefix[-1] in _INITIAL_AFTER))
    return out


def vocabulary(text: str) -> set[str]:
    return {t.lower() for t, _ in _tokens(text)}


@dataclass
class ResumeFacts:
    """Precomputed vocabulary of the master resume for fast checks."""

    vocab: set[str]
    numbers: set[str]
    tech: set[str]

    @classmethod
    def from_resume(cls, resume: MasterResume) -> "ResumeFacts":
        text = "\n".join(
            [resume.flatten(), resume.contact.name, resume.contact.location]
            + [e.company for e in resume.experience]
            + [e.location for e in resume.experience]
            + [p.name for p in resume.projects]
            + [f"{ed.school} {ed.location}" for ed in resume.education]
        )
        skill_tokens = {t for s in resume.all_skills() for t in vocabulary(s)}
        return cls(vocab=vocabulary(text), numbers=numbers_in(text), tech=TECH_TERMS | skill_tokens)


def _is_entity(token: str, initial: bool, tech: set[str]) -> bool:
    if token == "I":
        return False  # the pronoun, capitalized everywhere in prose
    if token.lower() in tech:
        return True
    if any(c.isdigit() for c in token) and any(c.isalpha() for c in token):
        return True  # EC2, S3, p99, H100
    if any(c.isupper() for c in token[1:]):
        return True  # PostgreSQL, gRPC, USDC, OpenAI
    return token[:1].isupper() and not initial  # proper noun mid-sentence


@dataclass
class Finding:
    numbers: list[str] = field(default_factory=list)
    tech: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.numbers or self.tech or self.names)

    def items(self) -> list[str]:
        return self.numbers + self.tech + self.names


def _check(
    candidate: str,
    source_numbers: set[str],
    source_vocab: set[str],
    names_vocab: set[str],
    tech: set[str],
) -> Finding:
    finding = Finding(numbers=sorted(numbers_in(candidate) - source_numbers))
    seen: set[str] = set()
    for token, initial in _tokens(candidate):
        low = token.lower()
        if low in seen or low.isdigit() or low in NUMBER_WORDS:
            continue
        seen.add(low)
        if not _is_entity(token, initial, tech):
            continue
        if low in tech:
            if low not in source_vocab:
                finding.tech.append(token)
        elif low not in source_vocab and low not in names_vocab:
            finding.names.append(token)
    return finding


def verify_rephrase(original: str, rephrase: str, facts: ResumeFacts) -> Finding:
    """What a rephrased bullet adds beyond its original (names may come from the resume)."""
    return _check(rephrase, numbers_in(original), vocabulary(original), facts.vocab, facts.tech)


_FIRST_PERSON = re.compile(r"\b(i|i'm|i've|i'd|my|me|myself)\b", re.I)
_STOPWORDS = {
    "about", "above", "across", "after", "again", "also", "among", "and", "been", "being",
    "both", "bring", "brings", "could", "each", "from", "have", "having", "here", "into",
    "like", "more", "most", "much", "only", "other", "over", "same", "some", "such", "than",
    "that", "their", "them", "then", "there", "these", "they", "this", "those", "through",
    "very", "want", "well", "were", "what", "when", "where", "which", "while", "will",
    "with", "within", "would", "your", "yours",
}


# Everyday professional prose. A letter saying "my experience" or "ensuring quality" claims
# nothing the resume lacks; only domain words ("subscription", "testing") count as borrowed.
_GENERIC_PROSE = {
    "ability", "abilities", "apply", "applying", "application", "applications", "background", "believe",
    "best", "build", "building", "business", "candidate", "challenge", "challenges", "challenging", "class",
    "closely", "collaborate", "collaborating", "collaboration", "communication", "company", "complex",
    "confident", "consideration", "contribute", "contributing", "contribution", "create", "creating",
    "cross", "culture", "current", "currently", "customer", "customers", "deep", "deliver", "delivering",
    "delivery", "design", "designing", "develop", "developing", "development", "drive", "driven", "driving",
    "dynamic", "eager", "effective", "effectively", "efficient", "efficiently", "engineer", "engineering",
    "engineers", "ensure", "ensuring", "environment", "environments", "excited", "exciting", "experience",
    "experienced", "experiences", "familiar", "familiarity", "fast", "feature", "features", "focus",
    "focused", "forward", "grade", "great", "grow", "growth", "hands", "help", "helping", "high", "highly",
    "impact", "impactful", "include", "includes", "including", "interest", "interested", "involve",
    "involved", "join", "joining", "knowledge", "lead", "leading", "learn", "learning", "level", "levels",
    "look", "looking", "love", "maintain", "maintaining", "make", "making", "mission", "opportunity",
    "opportunities", "operate", "operating", "paced", "part", "passion", "passionate", "position",
    "problem", "problems", "product", "products", "proficiency", "proficient", "provide", "providing",
    "quality", "quickly", "range", "recent", "role", "roles", "skill", "skills", "software", "solution",
    "solutions", "solve", "solving", "strong", "success", "successful", "support", "supporting", "team",
    "teams", "technical", "technologies", "thank", "tool", "tools", "understand", "understanding", "user",
    "users", "using", "values", "variety", "various", "wide", "work", "worked", "working", "year", "years",
}


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def unsupported_claims(sentence: str, facts: ResumeFacts, context: str, company: str = "") -> list[str]:
    """Words a sentence about the candidate borrows from the job posting but not the resume.

    "My background in subscription management" passes the entity check (no
    numbers, names, or tech) yet claims experience the resume never states.
    Two or more such borrowed words in one sentence means drop it.

    Every sentence counts as being about the candidate unless it names the
    company and has no I/my/me -- models dodge a first-person rule by writing
    pronoun-free, resume-style claims ("demonstrating a user-first approach").
    """
    about_company = bool(company) and company.lower() in sentence.lower()
    if about_company and not _FIRST_PERSON.search(sentence):
        return []
    resume = {_stem(w) for w in facts.vocab}
    posting = {_stem(w) for w in vocabulary(context)}
    borrowed = []
    # Lowercase words only: names (the company, its products) are the entity check's job.
    for word in re.findall(r"\b[a-z]+\b", sentence):
        if len(word) < 4 or word in _STOPWORDS or word in _GENERIC_PROSE:
            continue
        s = _stem(word)
        if s in posting and s not in resume and word not in borrowed:
            borrowed.append(word)
    return borrowed if len(borrowed) >= 2 else []


def verify_free_text(text: str, facts: ResumeFacts, allowed_context: str = "", company: str = "") -> Finding:
    """Check prose (cover letters, drafted answers) against the whole resume.

    `allowed_context` (company name, job title, the JD) may supply proper
    nouns -- a letter can name the company it is addressed to -- but never
    numbers or technologies, which would be claims about the candidate. The
    `company` itself is always allowed, even when it is also a tech name
    ("At Stripe", "At Datadog").
    """
    names = facts.vocab | (vocabulary(allowed_context) if allowed_context else set())
    own = vocabulary(company) if company else set()
    return _check(text, facts.numbers, facts.vocab | own, names, facts.tech)
