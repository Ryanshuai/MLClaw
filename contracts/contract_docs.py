"""The documents must agree with the disk, and with each other.

Every drift found while hardening this repo lived on the unenforced side of the
line: README said training was unbuilt while four training skills sat in
`skills/`; CLAUDE.md's skill table listed `/data-check`, which has never
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
import glob
import os
import re
import subprocess
import sys
import unittest

from helpers import REPO_ROOT

SKILLS_DIR = os.path.join(REPO_ROOT, "skills")
CLAUDE_MD = os.path.join(REPO_ROOT, "CLAUDE.md")
README = os.path.join(REPO_ROOT, "README.md")

# CLAUDE.md is loaded every session, so it holds routing and the headline rules
# only; detail lives in these, read on demand. A citation may target any of them.
DOC_FILES = {
    "CLAUDE.md": CLAUDE_MD,
    "run-mechanics.md": os.path.join(REPO_ROOT, "lifecycle", "references", "run-mechanics.md"),
    "layout.md": os.path.join(REPO_ROOT, "lifecycle", "references", "layout.md"),
    "skill-graph.md": os.path.join(REPO_ROOT, "lifecycle", "references", "skill-graph.md"),
    "data-line.md": os.path.join(REPO_ROOT, "lifecycle", "references", "data-line.md"),
    "fleet.md": os.path.join(REPO_ROOT, "lifecycle", "references", "fleet.md"),
}
LAYOUT_MD = DOC_FILES["layout.md"]
CONTRACTS_DIR = os.path.dirname(os.path.abspath(__file__))


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def skills_on_disk():
    """Skill dirs the repo actually carries — gitignored ones do not count.

    Same authority the layout check already defers to: `.gitignore` states what
    is not part of this project. Without it, a scaffolding tool that drops a
    working skill under `skills/` turns this red with a message about
    CLAUDE.md's table, sending the reader at the document when the problem is a
    directory that was never going to be committed."""
    if not os.path.isdir(SKILLS_DIR):
        return set()
    ignored = ignored_dir_names(REPO_ROOT)
    return {d for d in os.listdir(SKILLS_DIR)
            if d not in ignored
            and os.path.isfile(os.path.join(SKILLS_DIR, d, "SKILL.md"))}


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


def repo_tree_block(text):
    """The fenced tree under "### MLClaw repo" — the tool repo only. The workspace
    and user-project trees further down layout.md describe someone else's disk."""
    return text.split("### MLClaw repo", 1)[1].split("```")[1]


def declared_dirs(block):
    """-> repo-relative directories the tree names, including those implied by a
    file entry (`skills/eval-run/SKILL.md` declares `skills/eval-run`) and
    every ancestor (`.github/workflows/ci.yml` declares `.github` too)."""
    dirs, parents = set(), {}
    for line in block.splitlines():
        entry = line.split("←")[0].rstrip()
        name = entry.strip()
        if not name:
            continue
        indent = len(entry) - len(entry.lstrip())
        parents[indent] = name.rstrip("/")
        chain = [parents[i] for i in sorted(parents) if i < indent] + [name.rstrip("/")]
        path = "/".join(chain) if name.endswith("/") else os.path.dirname("/".join(chain))
        while path:
            dirs.add(path)
            path = os.path.dirname(path)
    return dirs


def ignored_dir_names(root):
    """The names git already knows are not part of this repo, by basename — the
    same granularity the walk below prunes at.

    `.gitignore` is the authoritative statement of "not part of this project",
    and the hand-written list in `actual_dirs` is a second copy of that idea that
    does not track edits to it. Any tool that drops a working directory in the
    repo root then turns this check red with a message about `layout.md`, which
    sends the reader at the documents when the problem is the environment.

    Falling back to the empty set on any git failure is safe by construction: the
    caller unions this with the hand list, so no git means exactly today's
    behaviour rather than a check that silently stops excluding anything."""
    try:
        p = subprocess.run(["git", "-C", root, "ls-files", "--others", "--ignored",
                            "--exclude-standard", "--directory"],
                           capture_output=True, text=True, encoding="utf-8", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return set()
    if p.returncode != 0:
        return set()
    return {line.rstrip("/").rsplit("/", 1)[-1]
            for line in p.stdout.splitlines() if line.endswith("/")}


def actual_dirs(root):
    """Everything on disk except build junk. `.git` is excluded by name rather
    than by a dotfile rule — `.claude` and `.github` are both real entries.

    A directory holding no file at all is skipped, and that is not laxity: git
    cannot represent an empty directory, so one can never reach a clone and
    `layout.md` documenting it would describe something no reader will ever see.
    They also slip past `ignored_dir_names` by construction — `git ls-files
    --others` lists paths, and an empty directory has none — so without this a
    stray `mkdir` in the working tree reads as an undocumented layout entry."""
    junk = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
            ".venv", ".pixi", "node_modules", ".egg-info"} | ignored_dir_names(root)
    found = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in junk]
        if not filenames:
            continue
        rel = os.path.relpath(dirpath, root).replace(os.sep, "/")
        # Every ancestor too: `skills` and `lifecycle/scripts` hold only
        # subdirectories, and they are real declared entries. What is being
        # excluded is a subtree with no file anywhere in it, not a container.
        while rel != ".":
            found.add(rel)
            rel = os.path.dirname(rel) or "."
    return found


def owned_by_a_skill(path):
    """Anything strictly below `skills/<name>/` is that skill's internal
    structure. The tree declares the skill dir; SKILL.md routes a reader inside."""
    parts = path.split("/")
    return parts[:1] == ["skills"] and len(parts) > 2


def citations_in(text):
    """-> [(doc, section)] cited by one file. `[^"\\n]` so the pattern cannot span
    lines and match the regex literal in this module's own source."""
    alt = "|".join(re.escape(d) for d in DOC_FILES)
    return re.findall(r'(%s) -> "([^"\n]+)"' % alt, text)


def pointers_in(text):
    """-> [(doc, quoted)] for the ARROW-LESS form: `CLAUDE.md "Script Integration"`.

    Skills overwhelmingly write it this way (`Per CLAUDE.md "Workflow State
    Protocol", push on entry`), and for a long time nothing checked it. Moving
    five sections into `skill-graph.md` left **twenty-six** skills pointing at
    headings CLAUDE.md no longer had, and the suite stayed green: `citations_in`
    only matches the ` -> ` form, which is a convention of `contracts/` and not
    of the skills.

    The quoted string is not required to be a heading here. Skills legitimately
    quote a single bullet — "Never record a metric you did not read" is a line in
    "Never silently", not a section of its own — so what gets checked is that the
    text still appears in that document at all, which is the property a reader
    actually depends on.
    """
    alt = "|".join(re.escape(d) for d in DOC_FILES)
    return re.findall(r'(%s)\s*"([^"\n]{3,70})"' % alt, text)


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


class LayoutTreeMatchesDisk(unittest.TestCase):
    """layout.md -> "File Layout": the tree is the map an agent reads instead of
    running `ls`, which is why a stale branch does not self-correct — whoever
    trusts it never looks. Three drifts were sitting in it at once: the six
    scripts under `scripts/infer-run/` had moved wholesale into `shared/` and
    the old path was still the one written down, the entire `lifecycle/training/`
    template dir was absent, and the root entry called CLAUDE.md "this file".

    Directory granularity, deliberately. Per-file alignment would turn every new
    script into a CI failure until the tree was edited, and a map maintained
    under duress stops being read as a map. A directory that moved, vanished, or
    appeared is the drift that actually sends a reader somewhere wrong.
    """

    def setUp(self):
        declared = declared_dirs(repo_tree_block(read(LAYOUT_MD)))
        self.declared = {d for d in declared if not owned_by_a_skill(d)}
        self.actual = {d for d in actual_dirs(REPO_ROOT) if not owned_by_a_skill(d)}

    def test_every_declared_directory_exists(self):
        self.assertEqual(sorted(self.declared - self.actual), [],
                         "layout.md's tree names directories that are not on disk")

    def test_every_directory_on_disk_is_declared(self):
        self.assertEqual(sorted(self.actual - self.declared), [],
                         "these directories exist but layout.md's tree never names them")


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

    def test_an_arrowless_pointer_still_names_something_that_exists(self):
        """`CLAUDE.md "Script Integration"` — the form skills actually use, and the
        one that was invisible here. See `pointers_in`: the target need not be a
        heading, but it has to still be in that file.

        **Prose only.** It used to walk `.py` too, and there the pattern is not a
        citation — it is any line where a document's filename is followed by a
        string literal, which is what `open(os.path.join(ROOT, "CLAUDE.md"),
        encoding="utf-8")` is. Adding an encoding argument to a file read reported
        a dangling pointer at a section named `), encoding=`, twice in one sitting,
        and the only way to satisfy it was to restructure working code. A check
        that fires on edits it has no opinion about spends the attention that the
        checks with real opinions need — so the half that misfires is gone, and the
        half that catches a skill citing a section somebody renamed is kept.
        """
        bodies = {doc: read(path) for doc, path in DOC_FILES.items()}
        dangling = []
        skip = (".git", "__pycache__", "node_modules")
        for dp, dirs, fs in os.walk(REPO_ROOT):
            dirs[:] = [d for d in dirs if d not in skip]
            for f in fs:
                if not f.endswith(".md"):
                    continue
                path = os.path.join(dp, f)
                if os.path.basename(path) == os.path.basename(__file__):
                    continue  # this module quotes the form in order to describe it
                try:
                    text = read(path)
                except (OSError, UnicodeDecodeError):
                    continue
                for doc, quoted in pointers_in(text):
                    if quoted not in bodies.get(doc, ""):
                        dangling.append(
                            f"{os.path.relpath(path, REPO_ROOT)}: {doc} {quoted!r}")
        self.assertEqual(sorted(set(dangling)), [],
                         "pointers at text that is no longer in that document")

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
        for dp, _, fs in os.walk(os.path.join(REPO_ROOT, "skills")):
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


class ScriptPathsAreResolvedNotAssumed(unittest.TestCase):
    """CLAUDE.md -> "Script Integration": scripts are invoked via
    `python <mlclaw_root>/lifecycle/scripts/…`, and `<mlclaw_root>` is resolved
    by `shared/workspaces.py tool` rather than assumed.

    A bare `python lifecycle/scripts/…` is correct only when the working
    directory happens to be this repo. That is a live question — the skills are
    also discoverable from `~/.claude/skills/`, where the working directory is
    whatever the user is standing in.

    ‼️ And the failure is silent, which is why it earns a check rather than a
    style note. CLAUDE.md's fallback rule says a script that cannot be run means
    "do the same work manually", so a wrong path does not surface as an error —
    it surfaces as an agent hand-rolling a `retention.py` refusal, a `graph.py
    check`, or an `evacuate.py clearance` that never ran. The flow still reads as
    working. That is the same shape as every rule in "Never silently".

    The repo was 32/20 split when this was written; the 20 were rewritten and
    this is what stops them coming back.
    """

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    BARE = re.compile(r"python3? lifecycle/scripts/")

    def _docs(self):
        for sub in ("*/SKILL.md", "*/references/*.md"):
            yield from glob.glob(os.path.join(self.ROOT, "skills", sub))

    def test_no_skill_invokes_a_script_by_a_cwd_relative_path(self):
        bad = []
        for path in sorted(self._docs()):
            with open(path, encoding="utf-8") as f:
                for n, line in enumerate(f, 1):
                    if self.BARE.search(line):
                        bad.append(f"{os.path.relpath(path, self.ROOT)}:{n}")
        self.assertEqual(bad, [], "invoke scripts as "
                                  "`python <mlclaw_root>/lifecycle/scripts/…` — a "
                                  "cwd-relative path fails silently into the "
                                  "manual fallback")

    def test_the_resolver_it_points_at_exists(self):
        """A convention naming a tool that is not there is worse than none."""
        self.assertTrue(os.path.exists(os.path.join(
            self.ROOT, "lifecycle", "scripts", "shared", "workspaces.py")))

    def test_claude_md_states_the_resolved_form(self):
        with open(os.path.join(self.ROOT, "CLAUDE.md"), encoding="utf-8") as f:
            claude = f.read()
        self.assertIn("<mlclaw_root>/lifecycle/scripts/", claude)


if __name__ == "__main__":
    if "--report" in sys.argv:
        sys.exit(report())
    unittest.main()
