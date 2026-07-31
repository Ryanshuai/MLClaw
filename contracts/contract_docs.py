"""The documents must agree with the disk, and with each other.

Every drift found while hardening this repo lived on the unenforced side of the
line: README said training was unbuilt while four training skills sat in
`.claude/skills/`; CLAUDE.md's skill table listed `/data-check`, which has never
existed; `/lease` existed and appeared in neither. None of that raises. Someone
just reads the wrong thing and believes it.

This file also closes the loop on the citation rule. Every check class names the
section it enforces, in one of the three documents in `DOC_FILES`, and
`CitationsResolve` asserts those sections are real. A citation pointing at a
heading that no longer exists is the same class of rot as everything above, one
level up — and it is the specific risk created by splitting the documentation:
CLAUDE.md now holds routing only, so a contract can move to a reference file and
leave its check silently pointing at nothing.

    python contracts/contract_docs.py --report

prints, per document, which sections have a check behind them and which do not.
That list is the honest scope of what a green run proves — regenerate it rather
than maintaining a copy by hand.
"""
import os
import re
import sys
import unittest

from helpers import REPO_ROOT

SKILLS_DIR = os.path.join(REPO_ROOT, ".claude", "skills")
CLAUDE_MD = os.path.join(REPO_ROOT, "CLAUDE.md")
README = os.path.join(REPO_ROOT, "README.md")

# CLAUDE.md is loaded every session, so it holds routing and the headline rules
# only; detail lives in these, read on demand. A citation may target any of them.
DOC_FILES = {
    "CLAUDE.md": CLAUDE_MD,
    "run-mechanics.md": os.path.join(REPO_ROOT, "lifecycle", "references", "run-mechanics.md"),
    "layout.md": os.path.join(REPO_ROOT, "lifecycle", "references", "layout.md"),
}
CONTRACTS_DIR = os.path.dirname(os.path.abspath(__file__))


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def skills_on_disk():
    if not os.path.isdir(SKILLS_DIR):
        return set()
    return {d for d in os.listdir(SKILLS_DIR)
            if os.path.isfile(os.path.join(SKILLS_DIR, d, "SKILL.md"))}


def skill_table_entries(text):
    """`/name` values from the Skills & Dependencies table only — not from prose,
    which legitimately names unbuilt skills to explain that they are unbuilt."""
    m = re.search(r"^\| Skill \| What it does \|$(.*?)(?=\n\n)", text, re.M | re.S)
    if not m:
        return set()
    return set(re.findall(r"^\|\s*`/([a-z0-9-]+)`\s*\|", m.group(1), re.M))


def readme_status(text):
    """-> (claimed_done, claimed_todo) as sets of skill names."""
    done, todo = set(), set()
    for mark, line in re.findall(r"^- \[([ x])\] (.*)$", text, re.M):
        (done if mark == "x" else todo).update(re.findall(r"`/([a-z0-9-]+)`", line))
    return done, todo


def claude_md_sections(text):
    return {m.group(2).strip(): m.group(1)
            for m in re.finditer(r"^(#{2,4}) (.+)$", text, re.M)}


def citations_in(text):
    """-> [(doc, section)] cited by one file. `[^"\\n]` so the pattern cannot span
    lines and match the regex literal in this module's own source."""
    alt = "|".join(re.escape(d) for d in DOC_FILES)
    return re.findall(r'(%s) -> "([^"\n]+)"' % alt, text)


def citations():
    """-> {section_name: [source files]} across every contract_*.py."""
    found = {}
    for fname in sorted(os.listdir(CONTRACTS_DIR)):
        if not (fname.startswith("contract_") and fname.endswith(".py")):
            continue
        for doc, section in citations_in(read(os.path.join(CONTRACTS_DIR, fname))):
            files = found.setdefault((doc, section), [])
            if fname not in files:
                files.append(fname)
    return found


class SkillTableMatchesDisk(unittest.TestCase):
    """CLAUDE.md -> "Skills & Dependencies": the table is an inventory of what
    exists. A skill listed but absent sends an agent to invoke nothing; a skill
    present but unlisted is invisible to every agent that reads only this file.
    """

    def setUp(self):
        self.disk = skills_on_disk()
        self.table = skill_table_entries(read(CLAUDE_MD))

    def test_disk_is_not_empty(self):
        self.assertTrue(self.disk, f"no SKILL.md found under {SKILLS_DIR}")

    def test_every_listed_skill_exists(self):
        self.assertEqual(sorted(self.table - self.disk), [],
                         "listed in CLAUDE.md's skill table but not on disk")

    def test_every_skill_on_disk_is_listed(self):
        self.assertEqual(sorted(self.disk - self.table), [],
                         "on disk but missing from CLAUDE.md's skill table")


class ReadmeStatusMatchesDisk(unittest.TestCase):
    """CLAUDE.md -> "Status": README's checklist is what a human reads to decide
    whether a capability exists. It drifted before — training shipped and the box
    stayed unchecked for four skills. README may omit internal utilities, so this
    checks claims, not completeness.
    """

    def setUp(self):
        self.disk = skills_on_disk()
        self.done, self.todo = readme_status(read(README))

    def test_nothing_claimed_done_is_missing(self):
        self.assertEqual(sorted(self.done - self.disk), [],
                         "README marks these done but they are not on disk")

    def test_nothing_claimed_todo_already_exists(self):
        self.assertEqual(sorted(self.todo & self.disk), [],
                         "README marks these unbuilt but they exist on disk")


class CitationsResolve(unittest.TestCase):
    """CLAUDE.md -> "Contracts": every check cites the section it enforces, and
    the citation is the admission rule. A citation naming a heading that does not
    exist means the contract was renamed or deleted while the check kept passing —
    at which point the check is enforcing something nobody has written down.
    """

    def setUp(self):
        self.sections = {doc: claude_md_sections(read(path)) for doc, path in DOC_FILES.items()}
        self.cited = citations()

    def test_every_citation_names_a_real_section(self):
        missing = sorted(f"{doc} -> {sec}" for doc, sec in self.cited
                         if sec not in self.sections.get(doc, {}))
        self.assertEqual(missing, [], "cited in contracts/ but no such heading in that document")

    def test_every_doc_file_exists(self):
        for doc, path in DOC_FILES.items():
            self.assertTrue(os.path.isfile(path), f"{doc} is missing at {path}")

    def test_claude_md_points_at_every_reference(self):
        """CLAUDE.md carries only routing; a reference it never names is unreachable."""
        text = read(CLAUDE_MD)
        for doc in DOC_FILES:
            if doc == "CLAUDE.md":
                continue
            self.assertIn(doc, text, f"CLAUDE.md never points a reader at {doc}")

    def test_nothing_in_the_repo_points_at_a_section_that_moved(self):
        """Skills, scripts and templates all cross-reference these documents by
        section name. The CLAUDE.md split moved most of those sections out; a
        stale pointer sends a reader to something that isn't there, and it reads
        as a working instruction rather than an error.

        Scoped to the whole repo, not just `.claude/`: when this check only
        walked the skill tree, nine dangling citations were sitting in
        `lifecycle/` — five of them in files added by the very change that moved
        the sections.
        """
        dangling = []
        skip = (".git", "__pycache__", "node_modules")
        for dp, dirs, fs in os.walk(REPO_ROOT):
            dirs[:] = [d for d in dirs if d not in skip]
            for f in fs:
                if not f.endswith((".md", ".py", ".json", ".yml")):
                    continue
                path = os.path.join(dp, f)
                try:
                    text = read(path)
                except (OSError, UnicodeDecodeError):
                    continue
                for doc, sec in citations_in(text):
                    if sec not in self.sections.get(doc, {}):
                        dangling.append(f"{os.path.relpath(path, REPO_ROOT)}: {doc} -> {sec!r}")
        self.assertEqual(sorted(dangling), [], "references to sections that do not exist")

    def test_no_skill_hand_writes_a_run_tree_query(self):
        """run-mechanics.md -> "Listing runs (no separate index)": go through
        `list_runs.py`, do not hand-write the jq.

        The `mode` filter is a correctness rule, and a correctness rule written
        as a snippet everyone retypes gets forgotten exactly once — silently, in
        the query that mattered. `list_runs.py` was added to make forgetting it a
        `TypeError`; that only holds while nothing routes around it. An agent
        reading a skill obeys the skill, so a stray `jq` here beats the doc.
        """
        offenders = []
        for dp, _, fs in os.walk(os.path.join(REPO_ROOT, ".claude")):
            for f in fs:
                if not f.endswith(".md"):
                    continue
                path = os.path.join(dp, f)
                for n, line in enumerate(read(path).splitlines(), 1):
                    if "jq" in line and re.search(r"runs/\S*run\.json|run_\*/run\.json", line):
                        offenders.append(f"{os.path.relpath(path, REPO_ROOT)}:{n}")
        self.assertEqual(offenders, [],
                         "hand-written jq over the run tree — call "
                         "lifecycle/scripts/shared/list_runs.py instead")

    def test_every_contract_file_cites_something(self):
        uncited = []
        for fname in sorted(os.listdir(CONTRACTS_DIR)):
            if fname.startswith("contract_") and fname.endswith(".py"):
                if not citations_in(read(os.path.join(CONTRACTS_DIR, fname))):
                    uncited.append(fname)
        self.assertEqual(uncited, [],
                         "contract files with no citation — either write the contract "
                         "into CLAUDE.md or delete the file")


def report():
    cited = citations()
    total = enforced_n = 0
    for doc, path in DOC_FILES.items():
        sections = claude_md_sections(read(path))
        total += len(sections)
        print(f"\n=== {doc} ({len(sections)} sections) ===")
        for sec, lvl in sections.items():
            who = cited.get((doc, sec))
            if who:
                enforced_n += 1
                print(f"  [x] {sec:<44} {', '.join(who)}")
            else:
                print(f"  [ ] {'  ' * (len(lvl) - 2)}{sec}")
    print(f"\n{enforced_n} of {total} sections have a check behind them.")
    print("Unchecked sections are not verified by anything — a green run says nothing about them.")
    return 0


if __name__ == "__main__":
    if "--report" in sys.argv:
        sys.exit(report())
    unittest.main()
